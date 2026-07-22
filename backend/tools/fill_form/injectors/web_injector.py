"""WebInjector — fills a web form by selector (Playwright for Python).

Leaves ``human_only`` fields blank, submits, and captures the confirmation. Lazy-
imports Playwright so the PDF path (and unit tests) never require a browser.
"""

from __future__ import annotations

from contracts.models import FormSchema


class WebInjector:
    async def inject(self, schema: FormSchema, values: dict, *, headless: bool = True, **_) -> dict:
        from playwright.async_api import async_playwright  # lazy: no browser needed for PDF/L1

        filled: list[str] = []
        confirmation = ""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()
            await page.goto(schema.source_ref)  # URL for web targets

            for field in schema.fillable_fields():  # NEVER human_only (§2)
                value = values.get(field.name)
                if value in (None, "") or not field.selector:
                    continue
                await page.fill(field.selector, str(value))
                filled.append(field.name)

            # Submit + capture whatever the page shows back as confirmation.
            submit = page.locator("button[type=submit], input[type=submit]")
            if await submit.count():
                await submit.first.click()
                await page.wait_for_load_state("networkidle")
            confirmation = (await page.inner_text("body"))[:2000]
            await browser.close()

        return {
            "target": "web",
            "url": schema.source_ref,
            "filled_fields": filled,
            "left_blank": [f.name for f in schema.fields if f.fill_policy == "human_only"],
            "confirmation": confirmation,
        }
