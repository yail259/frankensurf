"""A few safe, read-only browser actions that turn a page into the page.

Some pages hold their content behind one small action rather than a wall:
a cookie banner over the content, a "load more" button under the first
results, or a price that appears only once a dropdown has a value. run()
performs only these actions, chosen by web conventions (ARIA roles, button
words in common languages), never by site:

- dismiss_consent: inside a cookie or consent banner, press the reject or
  necessary-only button (the most privacy-preserving choice). Never accept;
  if there is no reject, close the banner.
- load_more: press "load more" / "show more" / "next" up to a few times while
  the page keeps growing.
- reveal: give required dropdowns that have no value their first real option.

Nothing is bought, booked, submitted, signed in to or typed. A guard refuses
any control whose words say otherwise. Every action is reported.
"""
from __future__ import annotations

import re

ACTIONS = ("dismiss_consent", "load_more", "reveal")

_CONSENT_BOX = re.compile(r"(?i)cookie|consent|gdpr|privacy|cmp|tracking|datenschutz")
_REJECT = re.compile(
    r"(?i)^\s*(reject( all)?|decline( all)?|deny( all)?|refuse( all)?|(use |allow )?(only |strictly )?"
    r"(necessary|essential|required)( cookies)?( only)?|continue without (accepting|agreeing)|"
    r"alle ablehnen|ablehnen|nur (notwendige|erforderliche|essenzielle)( cookies)?|tout refuser|refuser|"
    r"continuer sans accepter|rifiuta( tutto)?|rechazar( todo)?|alles weigeren|weigeren|"
    r"avvisa alla|afvis alle|avvis alle|odrzuć wszystkie|recusar( tudo)?)\s*$")
_CLOSE = re.compile(r"(?i)^\s*(close|dismiss|×|✕|x|schließen|fermer|chiudi|cerrar|sluiten|stäng|luk)\s*$")
_MORE = re.compile(
    r"(?i)^\s*((load|show|view|see) more( results| products| items)?|more results|next( page)?|›|»|"
    r"mehr (laden|anzeigen)|weitere (laden|anzeigen)|voir plus|afficher plus|ver más|cargar más|meer laden|"
    r"toon meer|mostra (altri|di più)|carica altri|visa fler|vis flere|näytä lisää)\s*$")
# Never press anything that commits, pays, signs in or shares.
_FORBIDDEN = re.compile(
    r"(?i)\b(buy|purchase|checkout|check out|pay|order|add to (cart|bag|basket|trolley)|book|reserve|"
    r"subscribe|sign ?(in|up)|log ?in|register|submit|send|share|delete|remove|accept|agree|allow all|"
    r"kaufen|bestellen|acheter|comprar|acquista|kopen|köp)\b")
_CLICKABLE = "button, [role=button], a[role=button], input[type=button], input[type=submit], a"


async def _label(element):
    try:
        text = await element.evaluate(
            "e => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim()")
    except Exception:
        return ""
    return " ".join(str(text).split())[:120]


async def _consent_roots(page):
    """Frames and containers that look like a consent banner."""
    roots = []
    for frame in page.frames:
        try:
            url = frame.url or ""
        except Exception:
            url = ""
        if frame is not page.main_frame and _CONSENT_BOX.search(url):
            roots.append(frame)
    selector = ",".join(f"[{attr}*='{word}' i]" for attr in ("id", "class", "aria-label")
                        for word in ("cookie", "consent", "gdpr", "cmp", "privacy"))
    try:
        boxes = await page.query_selector_all(selector + ", [role=dialog], [aria-modal=true]")
    except Exception:
        boxes = []
    for box in boxes[:30]:
        try:
            text = (await box.inner_text())[:3000]
        except Exception:
            continue
        if _CONSENT_BOX.search(text):
            roots.append(box)
    return roots


async def _click(element, page, label, log, action):
    if _FORBIDDEN.search(label):
        return False
    try:
        if not await element.is_visible():
            return False
        await element.click(timeout=3000)
        await page.wait_for_timeout(800)
    except Exception:
        return False
    log.append({"action": action, "label": label, "ok": True})
    return True


async def dismiss_consent(page, log):
    for root in await _consent_roots(page):
        try:
            buttons = await root.query_selector_all(_CLICKABLE)
        except Exception:
            continue
        labelled = [(button, await _label(button)) for button in buttons[:80]]
        for wanted in (_REJECT, _CLOSE):
            for button, label in labelled:
                if label and wanted.match(label) and await _click(button, page, label, log, "dismiss_consent"):
                    return True
    return False


async def load_more(page, log, times=3):
    pressed = 0
    for _ in range(times):
        try:
            before = await page.evaluate("document.body ? document.body.innerText.length : 0")
            buttons = await page.query_selector_all(_CLICKABLE)
        except Exception:
            break
        target = None
        for button in buttons[-400:]:
            label = await _label(button)
            if label and _MORE.match(label):
                target = (button, label)
                break
        if target is None or not await _click(target[0], page, target[1], log, "load_more"):
            break
        try:
            await page.wait_for_timeout(1200)
            after = await page.evaluate("document.body ? document.body.innerText.length : 0")
        except Exception:
            break
        pressed += 1
        if after <= before:
            log[-1]["grew"] = False
            break
    return pressed


async def reveal(page, log):
    """First real option for visible dropdowns that have no value yet."""
    changed = 0
    try:
        selects = await page.query_selector_all("select")
    except Exception:
        return 0
    for select in selects[:10]:
        try:
            if not await select.is_visible():
                continue
            state = await select.evaluate(
                "s => ({value: s.value, options: Array.from(s.options).map(o => ({v: o.value, t: o.text.trim(), d: o.disabled}))})")
        except Exception:
            continue
        real = [option for option in state["options"] if option["v"] and not option["d"]]
        if state["value"] or not real or _FORBIDDEN.search(real[0]["t"]):
            continue
        try:
            await select.select_option(real[0]["v"], timeout=3000)
            await page.wait_for_timeout(1000)
        except Exception:
            continue
        changed += 1
        log.append({"action": "reveal", "label": real[0]["t"][:120], "ok": True})
    return changed


async def run(page, actions) -> list[dict]:
    """Perform the requested actions in a safe order; the log of what was done."""
    log: list[dict] = []
    if "dismiss_consent" in actions:
        await dismiss_consent(page, log)
    if "reveal" in actions:
        await reveal(page, log)
    if "load_more" in actions:
        await load_more(page, log)
    return log[:20]
