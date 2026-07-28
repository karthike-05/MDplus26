-- 003_sw_selection_gate.sql
--
-- Make the social worker — not the scheduler — choose which service a referral goes to.
--
-- WHY. The product intent is: once the viable candidates are ranked, the SW sees all the
-- options, picks the most appropriate, and *that choice* both feeds the memory system
-- (`sw_feedback`, which trains Layer 3) and triggers the next step of the pipeline.
-- `advance_referral` did the opposite: step 7 silently took the top-ranked candidate and
-- dispatched outreach itself. The human was never asked, and the feedback loop that is
-- supposed to make ranking improve over time had no event to learn from.
--
-- WHAT. One new block, placed after the "no candidates -> rank" branch and before the
-- existing auto-select. Three cases, in order:
--
--   1. A candidate is already flagged `selected` -> ADOPT it. This is the SW's decision
--      arriving; the referral takes that service and moves on. Without this case the
--      function would fall through to step 7 and re-pick by rank, quietly overruling the
--      human — and worse, step 7 only considers `candidate_status='available'`, so the
--      SW's own pick (now 'selected') would be the one row it refused to consider.
--   2. An eligible candidate is still available and nobody has chosen -> queue
--      `select_resource` to **social_worker** and return `awaiting_sw_selection`. The
--      open-action guard then parks the referral until a human acts, which is exactly
--      the "wait for the SW" semantics we want and costs no extra machinery.
--   3. Neither -> fall through untouched, so the pre-existing "no candidate remains ->
--      escalate" logic in step 7 still runs verbatim.
--
-- IS THIS SAFE FOR THE OTHER SERVICES? Checked before applying:
--   * No component outside karthik_form calls advance_referral() or parses its return.
--     patient_comms references it only in comments; it polls `referral_actions` directly.
--   * patient_comms is doubly insulated from the new row: it filters on
--     assigned_component='twilio' AND an action_type allowlist.
--   * `select_resource` and `social_worker` are both already in their respective CHECK
--     constraints, so no constraint changes and no enum migration.
--   * Most importantly, the branch being displaced HAS NEVER EXECUTED in production:
--     `referral_service_candidates` has always been empty, no referral has ever reached
--     status='resource_selected', and no select_resource row has ever been queued. No
--     component can depend on behaviour that has never happened.
--   * Every other branch below is byte-identical to the deployed function.
--
-- THE ONE NEW OBLIGATION. A referral now parks on an open `select_resource` action
-- addressed to `social_worker`, and *nothing polls that component* — by design, a human
-- does. Our `POST /api/referrals/{id}/choose-service` is what completes it (marks the
-- candidate selected, records sw_feedback, closes the action, calls advance_referral).
-- If that endpoint is unavailable, referrals sit at `awaiting_sw_selection` until it
-- returns. That is a visible, named state on the Integration screen, not a silent stall.
--
-- Mirrored in MockReferralDB so the offline port can't drift (tests/test_worker.py).

create or replace function public.advance_referral(p_referral_id uuid)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare
 r referrals%rowtype; p patients%rowtype; c referral_service_candidates%rowtype;
 v_open int; v_attempts int; v_channel text; v_action text; v_component text; v_id uuid;
begin
 select * into r from referrals where id=p_referral_id for update;
 if not found then raise exception 'Referral % not found',p_referral_id; end if;
 select * into p from patients where id=r.patient_id;
 select count(*) into v_open from referral_actions where referral_id=r.id and action_status in ('ready','in_progress','blocked');
 if v_open>0 then return jsonb_build_object('state','waiting','reason','An action is already open'); end if;

 -- MILESTONE 2 (added by 002_utilization_milestone.sql): the service accepted; did the
 -- patient actually use it? Must precede the terminal-state check below, which would
 -- otherwise return early on 'enrolled'.
 if r.status='enrolled' and coalesce(r.completion_outcome,'') not in ('patient_confirmed_utilization','patient_did_not_utilize') then
   if r.patient_confirmed_utilization is true then
     update referrals set completion_outcome='patient_confirmed_utilization',
       completed_at=coalesce(r.patient_confirmed_at,now()),updated_at=now() where id=r.id;
     return jsonb_build_object('state','utilization_confirmed','reason','Patient confirmed they used the service');
   elsif r.patient_confirmed_utilization is false then
     update referrals set status='escalated',completion_outcome='patient_did_not_utilize',
       escalation_reason='Patient reported they did not use the service',updated_at=now() where id=r.id;
     v_id:=queue_referral_action(r.id,r.service_id,'escalate_to_social_worker','social_worker','escalate:not_utilized:'||r.id,'Patient reported they did not use the service');
     return jsonb_build_object('state','escalated','action_id',v_id);
   else
     v_id:=queue_referral_action(r.id,r.service_id,'confirm_service_utilization','twilio','utilization:'||r.id,'Service accepted; confirm the patient actually used the resource');
     return jsonb_build_object('state','awaiting_utilization','action_id',v_id);
   end if;
 end if;

 if r.status in ('enrolled','failed','escalated') then return jsonb_build_object('state',r.status,'reason','Terminal referral state'); end if;
 if p.consent_status='declined' then
   update referrals set status='failed',completion_outcome='consent_declined',completed_at=now(),updated_at=now() where id=r.id;
   return jsonb_build_object('state','failed','reason','Consent declined');
 elsif p.consent_status<>'confirmed' then
   update referrals set status='waiting_for_consent',updated_at=now() where id=r.id;
   v_id:=queue_referral_action(r.id,null,'confirm_consent','twilio','consent:'||r.id,'Consent must be confirmed before resource action');
   return jsonb_build_object('state','waiting_for_consent','action_id',v_id);
 end if;
 if exists(select 1 from attempts where referral_id=r.id and outcome='enrolled') then
   update referrals set status='enrolled',completed_at=now(),completion_outcome='resource_enrollment_confirmed',updated_at=now() where id=r.id;
   v_id:=queue_referral_action(r.id,r.service_id,'complete_referral','backend','complete:'||r.id,'An attempt recorded enrollment');
   return jsonb_build_object('state','enrolled','action_id',v_id);
 end if;
 if not exists(select 1 from referral_service_candidates where referral_id=r.id) then
   update referrals set status='ranking',updated_at=now() where id=r.id;
   v_id:=queue_referral_action(r.id,null,'rank_resources','backend','rank:'||r.id,'No candidate ranking exists');
   return jsonb_build_object('state','ranking','action_id',v_id);
 end if;

 -- ### 003: THE SOCIAL-WORKER SELECTION GATE #################################
 if r.service_id is null then
   -- (1) The SW has chosen. Adopt it rather than re-picking by rank.
   select * into c from referral_service_candidates where referral_id=r.id and selected limit 1;
   if found then
     update referrals set service_id=c.service_id,current_resource_rank=c.rank,status='resource_selected',updated_at=now() where id=r.id;
     return jsonb_build_object('state','resource_selected','service_id',c.service_id,'reason','Social worker selected this service');
   end if;
   -- (2) A shortlist exists and nobody has chosen -> wait for a human.
   if exists(select 1 from referral_service_candidates where referral_id=r.id and candidate_status='available' and eligibility_state in ('eligible','potentially_eligible','unknown')) then
     -- `ranking`, not `resource_selected`: nothing has been selected yet, and that is
     -- the whole point of this state. The status CHECK has no `awaiting_selection`
     -- value and widening it would force every other service to handle a status it has
     -- never seen (the same reason milestone 2 kept to `completion_outcome`). `ranking`
     -- is honest — the resource decision is still open — and the precise signal for the
     -- UI is the open select_resource action itself, not this column.
     update referrals set status='ranking',updated_at=now() where id=r.id;
     v_id:=queue_referral_action(r.id,null,'select_resource','social_worker','sw_select:'||r.id,'Ranked shortlist is ready; a social worker must choose a service');
     return jsonb_build_object('state','awaiting_sw_selection','action_id',v_id);
   end if;
   -- (3) Nothing available: fall through to the escalation path below, unchanged.
 end if;
 -- ###########################################################################

 if r.service_id is null then
   select * into c from referral_service_candidates where referral_id=r.id and candidate_status='available' and eligibility_state in ('eligible','potentially_eligible','unknown') order by rank limit 1 for update;
   if not found then
     update referrals set status='escalated',escalation_reason='No eligible or unexhausted resource remains',updated_at=now() where id=r.id;
     v_id:=queue_referral_action(r.id,null,'escalate_to_social_worker','social_worker','escalate:no_resource:'||r.id,'No candidate remains');
     return jsonb_build_object('state','escalated','action_id',v_id);
   end if;
   update referral_service_candidates set selected=false,candidate_status=case when candidate_status='selected' then 'available' else candidate_status end where referral_id=r.id;
   update referral_service_candidates set selected=true,candidate_status='selected',updated_at=now() where id=c.id;
   update referrals set service_id=c.service_id,current_resource_rank=c.rank,status='resource_selected',updated_at=now() where id=r.id;
   v_id:=queue_referral_action(r.id,c.service_id,'select_resource','backend','select:'||r.id||':'||c.service_id,'Selected highest-ranked available candidate',jsonb_build_object('rank',c.rank,'score',c.score));
   return jsonb_build_object('state','resource_selected','service_id',c.service_id,'action_id',v_id);
 end if;
 if exists(select 1 from attempts where referral_id=r.id and service_id=r.service_id and status in ('queued','started','sent','delivered')) then
   update referrals set status='waiting_for_response',updated_at=now() where id=r.id;
   return jsonb_build_object('state','waiting_for_response','reason','An attempt is pending');
 end if;
 select count(*) into v_attempts from attempts where referral_id=r.id and service_id=r.service_id;
 if v_attempts>=3 or not exists(
   select 1 from service_application_channels ch where ch.service_id=r.service_id
   and not exists(select 1 from attempts a where a.referral_id=r.id and a.service_id=r.service_id and a.channel=ch.channel)
 ) then
   update referral_service_candidates set selected=false,candidate_status='exhausted',updated_at=now() where referral_id=r.id and service_id=r.service_id;
   update referrals set service_id=null,current_resource_rank=null,status='in_progress',updated_at=now() where id=r.id;
   v_id:=queue_referral_action(r.id,null,'try_next_resource','backend','next:'||r.id||':'||coalesce(r.service_id::text,'none'),'Current resource has no unused channel or reached three attempts');
   return jsonb_build_object('state','try_next_resource','action_id',v_id);
 end if;
 select ch.channel into v_channel from service_application_channels ch where ch.service_id=r.service_id
 and not exists(select 1 from attempts a where a.referral_id=r.id and a.service_id=r.service_id and a.channel=ch.channel)
 order by ch.priority limit 1;
 if v_channel='online_form' then v_action:='prepare_online_form'; v_component:='karthik_form';
 elsif v_channel='phone' then v_action:='contact_service_by_phone'; v_component:='retell';
 elsif v_channel='email' then v_action:='contact_service_by_email'; v_component:='backend';
 else raise exception 'Unexpected channel %',v_channel; end if;
 update referrals set status='in_progress',updated_at=now() where id=r.id;
 v_id:=queue_referral_action(r.id,r.service_id,v_action,v_component,'attempt:'||r.id||':'||r.service_id||':'||v_channel,'Selected next unused channel by configured priority',jsonb_build_object('channel',v_channel,'attempt_number',v_attempts+1));
 return jsonb_build_object('state','in_progress','channel',v_channel,'attempt_number',v_attempts+1,'action_id',v_id);
end;$function$;
