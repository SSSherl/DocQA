"""Verify validation badges and dirty-state reset using mocked API results only."""
import copy
import json
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
        page=browser.new_page(viewport={'width':1420,'height':1000});errors=[];calls=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        settings={'allow_remote_models':True,'models':{r:{'enabled':True,'has_key':True,'base_url':'https://example.com/v1','url':'','model':'test-model','api_key_env':'','timeout':60} for r in ('chat','vision','embedding','reranker','bge','visual_embedding')}}
        state={'job':{'status':'idle'},'fail':False}
        page.route('**/api/model-settings',lambda r:r.fulfill(json=settings))
        page.route('**/api/model-settings/job',lambda r:r.fulfill(json=state['job']))
        def submit(route):
            body=route.request.post_data_json;calls.append(body)
            role=next(iter(body['models']))
            state['job']={'status':'failed' if state['fail'] else 'complete','result':{'message':'validation result','diagnostics':{'models':[{'role':role,'status':'unauthorized' if state['fail'] else 'available','message':'synthetic result'}]}}}
            route.fulfill(status=202,json={'status':'running'})
        page.route('**/api/model-settings/test',submit)
        page.route('**/api/model-settings/save',submit)
        page.goto('http://127.0.0.1:8765',wait_until='networkidle');page.locator('[data-tab=settings]').click()
        page.wait_for_function("document.querySelectorAll('.model-config').length===6")
        assert page.locator('.validation-check:not(.hidden)').count()==0
        for role in settings['models']:
            form=page.locator('#config-'+role)
            card=page.locator('.model-option').filter(has=form)
            if not form.is_visible():card.locator(':scope > summary').click()
            if role=='chat':form.locator('[name=key]').fill('dummy-new-key')
            form.locator('.test-button').click()
            page.wait_for_function("r=>!document.querySelector('#config-'+r+' .validation-check').classList.contains('hidden')",arg=role)
            assert card.locator('.validation-summary').is_visible()
            if role=='chat':
                assert form.locator('[name=key]').input_value()=='dummy-new-key','Testing must retain the new key for subsequent save'
                form.locator('button[type=submit]').click()
                page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
                assert calls[-1]['models']['chat']['key']=='dummy-new-key'
                assert form.locator('.validation-check').is_visible()
            form.locator('[name=model]').fill('changed-model')
            assert not form.locator('.validation-check').is_visible() and not card.locator('.validation-summary').is_visible()
            form.locator('.test-button').click()
            page.wait_for_function("r=>!document.querySelector('#config-'+r+' .validation-check').classList.contains('hidden')",arg=role)
            card.locator(':scope > summary').click()
            assert card.locator('.validation-summary').is_visible(), 'Collapsed header must retain successful check'
        form=page.locator('#config-chat');card=page.locator('.model-option').filter(has=form)
        if not form.is_visible():card.locator(':scope > summary').click()
        state['fail']=True
        form.locator('.test-button').click()
        page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
        assert not form.locator('.validation-check').is_visible() and not card.locator('.validation-summary').is_visible()
        page.locator('#models-remote').uncheck()
        assert page.locator('.validation-summary:not(.hidden)').count()==0
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()
        print('PASS: six success badges, collapsed checks, saved state, dirty reset, failed validation, retained draft key and mobile layout; mocked API only.')

if __name__=='__main__':main()
