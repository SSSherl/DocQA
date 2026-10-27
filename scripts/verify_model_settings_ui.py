"""Verify settings UX against the local deployment without calling cloud APIs."""
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
        page=browser.new_page(viewport={'width':1520,'height':1000})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto('http://127.0.0.1:8765',wait_until='networkidle')
        page.locator('[data-tab=settings]').click()
        page.wait_for_function("document.querySelectorAll('.model-config').length===6")
        assert page.locator('#retrieval-form').count()==0
        assert page.locator('.model-config input[type=password]').count()==6
        assert page.locator('#config-chat input[name=key]').input_value()==''
        assert page.locator('.model-option').count()==6
        assert page.locator('.model-option[open]').count()==0
        assert not page.locator('#config-embedding').is_visible()
        page.locator('.model-option').filter(has=page.locator('#config-embedding')).locator(':scope > summary').click()
        assert page.locator('#config-reranker').is_visible()
        assert not page.locator('#config-chat').is_visible()
        form=page.locator('#config-embedding')
        form.locator('.template-button').click()
        assert form.locator('[name=model]').input_value()=='Qwen3-Embedding-8B'
        assert form.locator('[name=base_url]').input_value()=='https://ai.gitee.com/v1'
        assert form.locator('[name=enabled]').is_checked()
        form.locator('[name=enabled]').uncheck()
        # Testing a disabled role must not make a request.
        sent=[]
        page.on('request',lambda req:sent.append(req.url) if '/api/model-settings/test' in req.url else None)
        form.locator('.test-button').click()
        assert not sent
        # Collapsing preserves the draft and toggles its desktop row together.
        summary=page.locator('.model-option').filter(has=form).locator(':scope > summary')
        summary.click()
        assert not form.is_visible()
        assert not page.locator('#config-reranker').is_visible()
        summary.click()
        assert form.locator('[name=model]').input_value()=='Qwen3-Embedding-8B'
        page.locator('.model-option').filter(has=page.locator('#config-chat')).locator(':scope > summary').click()
        # Explicitly save Chat as disabled, independently of the default draft selection.
        page.locator('#config-chat [name=enabled]').uncheck()
        page.locator('#config-chat button[type=submit]').click()
        page.wait_for_function("document.querySelector('#model-job').textContent.includes('配置已保存并生效')")
        assert page.locator('#config-chat input[name=key]').input_value()==''
        assert not page.locator('#config-chat input[name=enabled]').is_checked()
        state=page.request.get('http://127.0.0.1:8765/api/model-settings').json()
        assert all(not e.get('enabled') and not e['has_key'] for e in state['models'].values())
        assert form.locator('[name=model]').input_value()=='Qwen3-Embedding-8B', 'Other unsaved forms must be preserved'
        Path('data').mkdir(exist_ok=True)
        assert page.locator('#config-vision').is_visible()
        bge_summary=page.locator('.model-option').filter(has=page.locator('#config-bge')).locator(':scope > summary')
        assert bge_summary.inner_text().startswith('补充检索 · BGE-M3')
        bge_summary.click()
        assert page.locator('#config-bge').is_visible() and page.locator('#config-visual_embedding').is_visible()
        page.locator('.model-option').filter(has=page.locator('#config-visual_embedding')).locator(':scope > summary').click()
        assert not page.locator('#config-bge').is_visible() and not page.locator('#config-visual_embedding').is_visible()
        page.screenshot(path='data/model-settings-desktop.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        bge_summary.click()
        assert page.locator('#config-bge').is_visible()
        assert not page.locator('#config-visual_embedding').is_visible(), 'Single-column rows toggle independently'
        bge_summary.press('Enter')
        assert not page.locator('#config-bge').is_visible()
        page.screenshot(path='data/model-settings-mobile.png',full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Mobile horizontal overflow'
        assert not errors,errors
        browser.close()
        print('Six collapsed advanced options, expand/collapse, preserved drafts, live save and mobile layout passed. No cloud calls.')

if __name__=='__main__': main()
