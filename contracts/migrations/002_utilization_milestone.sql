-- 002_utilization_milestone.sql
--
-- Teach the DB scheduler about MILESTONE 2: the patient actually used the service.
--
-- WHY. Two different signals close a referral (CLAUDE.md §7) and they must not be
-- collapsed: the service *accepting* (status='enrolled'), and the patient *actually
-- using* the resource. Before this change `advance_referral` only knew the first —
-- 'enrolled' was terminal, so it returned "Terminal referral state" and never looked
-- further. Messaging was already collecting the second signal into
-- `referrals.patient_confirmed_utilization` / `patient_confirmed_at` via its own timed
-- loop, and `confirm_service_utilization` existed in the action_type enum, but nothing
-- ever queued it and no scheduler read those columns. So milestone 2 lived only inside
-- the messaging service and never landed on the referral.
--
-- WHAT. One new branch, placed before the terminal-state check. Every pre-existing
-- branch is byte-identical, so no current path changes behaviour:
--   * patient_confirmed_utilization IS TRUE  -> stamp completion_outcome +
--     completed_at. The referral is genuinely, verifiably closed.
--   * IS FALSE -> escalate to a social worker: the service accepted but the patient
--     never got the help, which is exactly the case a human must chase.
--   * IS NULL  -> queue `confirm_service_utilization` to twilio, so the check-in is
--     driven by the shared bus instead of only by Messaging's internal timers.
--
-- DESIGN NOTE — why no new `referrals.status` value. A value like 'completed' would be
-- the cleanest way to express this, but `status` carries a CHECK constraint that every
-- service reads and several switch on; widening it risks other components hitting a
-- status they don't recognise. `completion_outcome` is free text and already carries
-- 'consent_declined' / 'resource_enrollment_confirmed', so the milestone is recorded
-- there and the returned jsonb reports state='utilization_confirmed' for callers.
-- Revisit if the team wants a first-class terminal status.
--
-- SAFETY. CREATE OR REPLACE, additive, idempotent — safe to run more than once.
-- Guarded by `coalesce(completion_outcome,'') not in (...)` so it fires exactly once
-- per referral: a NULL comparison would otherwise make the whole condition NULL and
-- the branch would never run at all.

CREATE OR REPLACE FUNCTION public.advance_referral(p_referral_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
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
