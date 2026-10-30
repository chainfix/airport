"""Optional real-browser smoke test: pip install playwright; playwright install chromium.
Uses isolated temporary data and does not contact an Android device or DNS provider.
"""
import asyncio
import os
import tempfile
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright, expect

from airport.web import create_app


async def main():
    with tempfile.TemporaryDirectory(prefix="airport-browser-") as folder:
        app = create_app(folder, host="127.0.0.1")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with async_playwright() as playwright:
                options = {"headless": True, "args": ["--no-sandbox"]}
                if os.getenv("CHROMIUM_PATH"):
                    options["executable_path"] = os.environ["CHROMIUM_PATH"]
                browser = await playwright.chromium.launch(**options)
                context = await browser.new_context(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{port}")
                await expect(page.locator('#auth-title')).to_have_text('设置管理密码')
                await expect(page.locator('#auth-form input[name=code]')).to_have_count(0)
                await page.locator('#auth-form input[name=password]').fill('browser-test-password-123')
                await page.locator('#auth-submit').click()
                await expect(page.locator('#shell')).to_be_visible()
                await page.locator('#config-form input[name=android_host]').fill('192.168.1.50')
                await page.locator('#config-form input[name=domain]').fill('https://phone.example.com')
                await page.get_by_role('button', name='保存连接配置', exact=True).click()
                await expect(page.locator('#config-form [data-error=domain]')).to_contain_text('不要带')
                assert app['service'].store.get('config') is None
                await page.locator('#config-form input[name=domain]').fill('phone.example.com')
                await page.get_by_role('button', name='保存连接配置', exact=True).click()
                await expect(page.locator('#phone-domain')).to_have_text('phone.example.com')
                await expect(page.locator('#config-form [data-error=domain]')).to_be_empty()
                await page.locator('#start-gateway').click()
                await expect(page.locator('#toast')).to_contain_text('还没有证书')
                assert not app['service'].gateway.servers
                await page.get_by_role('tab', name='自动申请与续期').click()
                await page.locator('#dns-provider').select_option('alidns')
                await expect(page.locator('input[name=ALICLOUD_ACCESS_KEY]')).to_be_visible()
                await page.locator('#dns-provider').select_option('dnspod')
                await expect(page.locator('#dns-help')).to_contain_text('ID,Token')
                await page.get_by_role('tab', name='导入已有证书').click()
                await page.locator('#toast').wait_for(state='hidden', timeout=10000)
                await page.evaluate('window.scrollTo(0,0)')
                Path('docs').mkdir(exist_ok=True)
                await page.screenshot(path='docs/panel.png')
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.evaluate('window.scrollTo(0,0)')
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
                await page.screenshot(path='docs/mobile.png')
                await page.set_viewport_size({'width': 1440, 'height': 1100})
                await page.locator('#logout').click()
                await expect(page.locator('#auth-title')).to_have_text('欢迎回来')
                await page.locator('#auth-form input[name=password]').fill('browser-test-password-123')
                await page.locator('#auth-submit').click()
                await expect(page.locator('#phone-domain')).to_have_text('phone.example.com')
                assert not errors, errors
                await browser.close()
                print('Browser passed: setup, field validation, persistence, missing-certificate guard, provider fields, login, desktop/mobile layout, no JavaScript errors.')
        finally:
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
