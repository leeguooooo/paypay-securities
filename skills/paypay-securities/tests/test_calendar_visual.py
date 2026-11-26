"""Synthetic browser regression: uv run --with playwright --with pillow python tests/test_calendar_visual.py.

Uses local Chrome (CHROME_PATH may override). Screenshots stay in memory; no
financial data or golden snapshots are read or modified.
"""
import json
import os
from pathlib import Path
from io import BytesIO
from PIL import Image
from playwright.sync_api import sync_playwright

TEMPLATE = Path(__file__).resolve().parents[1] / 'paypay_sec/calendar_template.html'
record = lambda pnl, start, assets: {'pnl': pnl, 'comparison_from': start, 'total_assets': assets, 'data_quality': 'full'}
data = {'accounts': ['a', 'b'], 'generated_at': '', 'days': [
    {'date': '2026-01-31', 'accounts': {'a': record(100, '2026-01-30', 1000)}},
    {'date': '2026-02-02', 'accounts': {'a': record(200, '2026-01-31', 1000), 'b': record(300, '2026-01-31', 2000)}},
    {'date': '2026-02-03', 'accounts': {'a': record(400, '2026-02-02', 1000)}},
]}
html = TEMPLATE.read_text()
start = html.index('/* DATA_START */') + len('/* DATA_START */')
end = html.index('/* DATA_END */', start)
html = html[:start] + ' ' + json.dumps(data) + ' ' + html[end:]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get('CHROME_PATH', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
    page.set_content(html)
    page.locator('[data-acct="total"]').click()
    cell = page.locator('[data-date="2026-02-03"]')
    assert 'partial' in cell.get_attribute('class')
    assert 'linear-gradient' in cell.evaluate('(e) => getComputedStyle(e).backgroundImage')
    image = Image.open(BytesIO(cell.screenshot())).convert('RGB')
    colors = {image.getpixel((x, y)) for x in range(12, min(image.width-12, 42)) for y in range(12, min(image.height-12, 42))}
    assert len(colors) > 2, 'partial stripes absent from rendered screenshot'
    cell.hover()
    assert '收盘资产（部分）' in page.locator('#tip').inner_text()
    assert '0 个可计入日' in page.locator('.month-summary').inner_text()
    page.locator('#prevBtn').click()
    assert '2026年1月' in page.locator('#periodLabel').inner_text()
    page.locator('#nextBtn').click()
    assert '2026年2月' in page.locator('#periodLabel').inner_text()
    page.evaluate('state.cursor = new Date(2026, 0, 31); render()')
    page.locator('#nextBtn').click()
    assert '2026年2月' in page.locator('#periodLabel').inner_text()
    page.locator('[data-view="year"]').click()
    cell = page.locator('[data-date="2026-02-03"]')
    assert 'linear-gradient' in cell.evaluate('(e) => getComputedStyle(e).backgroundImage')
    assert len(cell.screenshot()) > 0
    browser.close()
print('calendar visual regressions passed (month/year screenshots, tooltip, navigation)')
