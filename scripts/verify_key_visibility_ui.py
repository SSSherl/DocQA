"""Exercise key visibility with synthetic keys; never reveal or modify real credentials."""
import json
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
        page=browser.new_page(viewport={'width':1520,'height':1000})
        errors=[];reveals=[];saves=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def reveal(route):
            reveals.append(route.request.post_data_json)
            route.fulfill(json={'key':'dummy-stored-secret'})
        def save(route):
            saves.append(route.request.post_data_json)
            route.fulfill(status=202,json={'status':'running'})
        page.route('**/api/model-settings/reveal',reveal)
        page.route('**/api/model-settings/save',save)
        page.route('**/api/model-settings/job',lambda r:r.fulfill(json={'status':'complete','result':{'status':'activated','message':'配置已保存并生效'}}))
        page.goto('http://127.0.0.1:8765',wait_until='networkidle')
        page.locator('[data-tab=settings]').click()
        page.wait_for_function("document.querySelectorAll('.key-toggle').length===6")
        assert not reveals
        form=page.locator('#config-chat')
        summary=page.locator('.model-option').filter(has=form).locator(':scope > summary')
        summary.click()
        key=form.locator('[name=key]'); eye=form.locator('.key-toggle')
        assert key.input_value()=='' and key.get_attribute('placeholder')=='********'
        assert key.get_attribute('type')=='password'
        eye.click()
        page.wait_for_function("document.querySelector('#config-chat [name=key]').type==='text'")
        assert key.input_value()=='dummy-stored-secret' and len(reveals)==1
        assert eye.get_attribute('aria-pressed')=='true'
        eye.click()
        assert key.input_value()=='' and key.get_attribute('type')=='password'
        eye.click()
        page.wait_for_function("document.querySelector('#config-chat [name=key]').type==='text'")
        form.locator('button[type=submit]').click()
        page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
        assert saves[-1]['models']['chat']['key']=='', 'Revealed stored credentials must be retained without rewriting'
        assert key.input_value()=='' and key.get_attribute('type')=='password'
        key.fill('dummy-new-secret')
        eye.click()
        assert key.get_attribute('type')=='text' and len(reveals)==2
        eye.click()
        assert key.get_attribute('type')=='password' and key.input_value()=='dummy-new-secret'
        eye.click()
        summary.click()
        page.wait_for_function("document.querySelector('#config-chat [name=key]').type==='password'")
        assert key.input_value()=='dummy-new-secret'
        summary.click()
        form.locator('button[type=submit]').click()
        page.wait_for_function("!document.querySelector('#config-chat button[type=submit]').disabled")
        assert saves[-1]['models']['chat']['key']=='dummy-new-secret'
        assert key.input_value()=='' and key.get_attribute('type')=='password'
        # Every role has an independent control, with no eager credential retrieval.
        for role in ['vision','embedding','reranker','bge','visual_embedding']:
            other=page.locator('#config-'+role)
            card=page.locator('.model-option').filter(has=other)
            if not other.is_visible():card.locator(':scope > summary').click()
            other.locator('.key-toggle').click()
            page.wait_for_function("r=>document.querySelector('#config-'+r+' [name=key]').type==='text'",arg=role)
            assert reveals[-1]['role']==role
            other.locator('.key-toggle').click()
            assert other.locator('[name=key]').input_value()==''
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()
        print('PASS: star placeholders, six reveal toggles, conceal, retained/replaced secrets, collapse and mobile. Only dummy credentials used.')

if __name__=='__main__':main()
