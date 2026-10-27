"""Optional browser acceptance test: RUN_BROWSER_AUDIT=1 ... -m unittest tests.test_browser_ui."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - dependency is optional for normal unit runs
    expect = None


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("RUN_BROWSER_AUDIT") == "1" and expect is not None,
                     "set RUN_BROWSER_AUDIT=1 after installing Playwright to run browser acceptance")
class BrowserAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(
            os.environ,
            WORKBENCH_OFFLINE="1",
            WORKBENCH_DB=str(Path(cls.temp.name) / "browser.sqlite3"),
            WORKBENCH_OUTPUT=str(Path(cls.temp.name) / "exports"),
        )
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(cls.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for _ in range(100):
            try:
                with urlopen(cls.base + "/health", timeout=2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            cls.process.terminate()
            cls.process.wait(timeout=10)
            raise RuntimeError("browser audit server did not start")
        cls.runtime = sync_playwright().start()
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if not executable:
            candidates = sorted(
                (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob(
                    "chromium-*/chrome-*/chrome.exe"
                ),
                reverse=True,
            )
            executable = str(candidates[0]) if candidates else None
        cls.browser = cls.runtime.chromium.launch(headless=True, executable_path=executable)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        self.page = self.context.new_page()
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.server_errors: list[tuple[str, int]] = []
        self.draft_saves: list[tuple[int, str, str]] = []
        self.step = "start"
        self.not_found: list[str] = []
        self.page.on("console", lambda msg: self.console_errors.append(
            f"{msg.text} @ {msg.location.get('url', '')}"
        ) if msg.type == "error" else None)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        self.page.on("response", lambda response: self.server_errors.append((response.url, response.status))
                     if response.status >= 500 else None)
        self.page.on("response", lambda response: self.not_found.append(response.url)
                     if response.status == 404 else None)
        self.page.on("response", self.capture_draft_save)

    def capture_draft_save(self, response):
        if "/api/drafts/" in response.url and response.request.method == "PATCH":
            try:
                body = response.text()[:1000]
            except Exception:
                body = "<unavailable>"
            self.draft_saves.append((response.status, self.step, body))

    def tearDown(self):
        self.context.close()

    def wait_idle(self):
        expect(self.page.locator(".task-progress")).to_have_count(0, timeout=30_000)

    def mock_query_result(self, snapshot):
        task_id = "mock-query-" + snapshot["id"]

        def tasks(route):
            if route.request.method != "POST":
                route.continue_()
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "id": task_id,
                    "kind": "query",
                    "status": "done",
                    "progress": 0,
                    "total": 0,
                    "message": "完成",
                    "result": {"snapshot_id": snapshot["id"]},
                    "created_at": "2026-10-23T10:00:00+08:00",
                    "updated_at": "2026-10-23T10:00:01+08:00",
                }, ensure_ascii=False),
            )

        self.page.route(re.compile(r".*/api/tasks$"), tasks)
        self.page.route(
            re.compile(r".*/api/snapshots/" + re.escape(snapshot["id"]) + r"$"),
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(snapshot, ensure_ascii=False),
            ),
        )

    def open_mock_query(self):
        self.page.goto(self.base, wait_until="networkidle")
        self.page.get_by_role("button", name="选取范围").click()
        self.page.get_by_role("button", name="检索待开记录").click()
        self.wait_idle()

    def test_numbered_domestic_voucher_displays_erp_english_customer(self):
        draft = {
            "id": "english-customer", "version": 1, "customs_no": "26MT-03X499",
            "business_type": "内销", "business_no": "26MT-03X499", "contracts": ["26MT-03X499"],
            "search_index": "26MT03X499", "source": [], "effective_source": [], "overrides": {},
            "header": {"posting_date": "2026-10-27", "category": "记", "maker": "测试"},
            "rows": [{"id": "ar", "摘要": "26MT-03X499内销收入", "科目编码": "112201",
                      "客户编码": "010249", "币种名称": "CNY", "汇率": "1", "借方金额": "100",
                      "贷方金额": "0", "原币借方": "100", "原币贷方": "0"}],
            "customer_labels": {"010249": "KOCH Engineered Solution"},
            "issues": [], "manual_fields": [], "valid": True, "exports": [],
            "totals": {"debit": "100.00", "credit": "0.00", "difference": "100.00"},
            "updated_at": "2026-10-27T12:00:00+08:00",
        }
        batch = {"id": "english-batch", "draft_ids": [draft["id"]],
                 "created_at": draft["updated_at"], "drafts": [draft]}
        def batches(route):
            value = batch if "/batches/" in route.request.url else [batch]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(value, ensure_ascii=False))
        self.page.route("**/api/batches**", batches)
        self.page.goto(self.base, wait_until="networkidle")
        customer = self.page.get_by_label("客户编码", exact=True)
        expect(customer).to_have_value("010249 KOCH Engineered Solution")
        customer.focus()
        expect(customer).to_have_value("010249")
        self.page.locator(".canvas-identity").click()
        expect(customer).to_have_value("010249 KOCH Engineered Solution")
        self.assertEqual(self.page_errors, [])

    def test_source_preview_formats_feishu_rich_fields_as_readable_text(self):
        self.mock_query_result({
            "id": "readable-source", "created_at": "2026-10-27T10:00:00+08:00",
            "mode": "pending", "matched": 1, "excluded": 0, "offline": True,
            "groups": [{"key": "内销:26MT-08C406", "business_type": "内销",
                        "business_no": "26MT-08C406", "contracts": ["26MT-08C406"],
                        "count": 2, "added": 1, "blocked": [], "hard_blocked": [],
                        "records": [
                            {"合同号_1": [{"text": "26MT-08C406", "type": "text"}],
                             "报关品名": "镍基退火软线", "是否开凭证": "待开"},
                            {"合同号_1": {"type": 1, "value": [{"text": "26MT-08C406"}]},
                             "报关品名": [{"text": "镍基退火软线"}], "是否开凭证": ""}]}],
        })
        self.open_mock_query()
        preview = self.page.locator(".range-modal details")
        preview.locator("summary").click()
        expect(preview.locator("p").nth(0)).to_have_text("26MT-08C406 · 镍基退火软线 · 待开")
        expect(preview.locator("p").nth(1)).to_have_text("26MT-08C406 · 镍基退火软线 · 未标记")
        expect(preview).not_to_contain_text('"text"')
        self.page.screenshot(path="tmp/browser-source-preview.png", full_page=True)
        self.assertEqual(self.page_errors, [])

    def test_empty_pending_result_requires_confirmation(self):
        self.mock_query_result({
            "id": "empty",
            "created_at": "2026-10-23T10:00:00+08:00",
            "mode": "pending",
            "groups": [],
            "matched": 0,
            "excluded": 3,
            "source": "测试飞书",
            "offline": True,
        })
        self.open_mock_query()
        expect(self.page.get_by_role("heading", name="当前没有待开的凭证")).to_be_visible()
        expect(self.page.get_by_text("本次检索已完成，无需构建凭证草稿", exact=False)).to_be_visible()
        expect(self.page.get_by_role("button", name=re.compile("构建凭证草稿"))).to_have_count(0)
        Path("tmp/browser-empty-query.png").parent.mkdir(exist_ok=True)
        self.page.screenshot(path="tmp/browser-empty-query.png", full_page=True)
        self.page.get_by_role("button", name="确认完成").click()
        expect(self.page.locator(".range-modal")).not_to_be_visible()
        expect(self.page.get_by_text("检索已完成：当前没有待开的凭证", exact=True)).to_be_visible()

    def test_all_hard_blocked_result_explains_required_fix(self):
        self.mock_query_result({
            "id": "blocked",
            "created_at": "2026-10-23T10:00:00+08:00",
            "mode": "pending",
            "groups": [{
                "key": "外销:missing:test",
                "business_type": "外销",
                "business_no": "",
                "customs_no": "",
                "contracts": ["26MT-MISSING-ID"],
                "search_index": "26MT-MISSING-ID",
                "count": 1,
                "matched": 1,
                "added": 0,
                "blocked": ["缺少报关单号，请在飞书补充后重新检索"],
                "hard_blocked": ["缺少报关单号，请在飞书补充后重新检索"],
                "records": [],
            }],
            "matched": 1,
            "excluded": 0,
            "source": "测试飞书",
            "offline": True,
        })
        self.open_mock_query()
        expect(self.page.get_by_role("heading", name="发现的凭证均需先修复")).to_be_visible()
        expect(self.page.get_by_text("请先在飞书修复数据，再重新检索", exact=False)).to_be_visible()
        expect(self.page.get_by_role("button", name=re.compile("构建凭证草稿"))).to_have_count(0)
        self.page.get_by_role("button", name="确认并关闭").click()
        expect(self.page.locator(".range-modal")).not_to_be_visible()

    def test_workbench_end_to_end_and_mobile_layout(self):
        page = self.page
        page.goto(self.base, wait_until="networkidle")
        expect(page).to_have_title(re.compile("凭证"))
        expect(page.get_by_text("按日期查未开", exact=True)).to_have_count(0)
        expect(page.get_by_label("开始日期")).to_have_count(0)
        expect(page.get_by_label("结束日期")).to_have_count(0)

        page.get_by_role("button", name="选取范围").click()
        expect(page.get_by_text("飞书待开范围", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="检索待开记录")).to_be_visible()
        expect(page.get_by_label("制单日期")).not_to_have_value("")
        page.get_by_role("button", name="检索待开记录").click()
        expect(page.locator(".modal h2")).to_contain_text("找到")
        page.get_by_label("全选可生成凭证").uncheck()
        expect(page.locator(".modal-footer")).to_contain_text("请至少选择一张可构建的凭证")
        expect(page.get_by_role("button", name=re.compile("构建凭证草稿"))).to_be_disabled()
        page.get_by_label("选择26MT-02C355").check()
        page.get_by_label("选择26MT-03R036F").check()
        page.get_by_role("button", name=re.compile("构建凭证草稿")).click()
        self.wait_idle()
        expect(page.locator(".voucher-item")).to_have_count(2)

        expect(page.get_by_role("button", name=re.compile("^删除凭证 "))).to_have_count(0)
        page.get_by_label("选择历史批次").click()
        page.locator(".batch-delete").first.click()
        delete_modal = page.locator(".delete-batch-modal")
        expect(delete_modal).to_be_visible()
        page.wait_for_timeout(400)
        box = delete_modal.bounding_box()
        self.assertIsNotNone(box)
        viewport = page.viewport_size
        self.assertLess(abs((box["y"] + box["height"] / 2) - viewport["height"] / 2), 8)
        Path("tmp/browser-delete-generation.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-delete-generation.png", full_page=True)
        page.get_by_role("button", name="确认删除").click()
        expect(delete_modal).not_to_be_visible()
        expect(page.locator(".voucher-item")).to_have_count(0)

        page.get_by_role("button", name="检索待开记录").click()
        expect(page.locator(".modal h2")).to_contain_text("找到")
        page.get_by_label("全选可生成凭证").uncheck()
        page.get_by_label("选择26MT-02C355").check()
        page.get_by_role("button", name=re.compile("构建凭证草稿")).click()
        self.wait_idle()
        expect(page.locator(".voucher-item")).to_have_count(1)

        page.get_by_label("合同号搜索").fill("02C355")
        expect(page.locator(".voucher-item")).to_have_count(1)
        page.locator(".voucher-item").click()
        expect(page.locator(".canvas-identity b")).to_contain_text("02C355")
        expect(page.locator(".paper-status")).to_contain_text("校验通过")
        expect(page.locator(".business-tag").first).to_contain_text("外销")
        expect(page.get_by_title("适应宽度")).to_have_count(0)
        expect(page.get_by_role("button", name="专注核对")).to_be_visible()
        zero_amount = page.locator('.voucher-grid tbody tr').nth(1).locator('input[aria-label="借方金额"]')
        zero_amount.click()
        expect(zero_amount).to_have_value("0")
        zero_amount.fill("=0+1")
        zero_amount.press("Enter")
        expect(zero_amount).to_have_value("1.00")
        page.locator(".canvas-identity").click()
        expect(page.locator('[title="请输入有效数值"]')).to_have_count(0)
        amount_input = page.locator('input[aria-label="借方金额"]').first
        amount_input.click()
        amount_input.fill("123.45")
        amount_input.press("Enter")
        expect(amount_input).to_have_value("123.45")
        page.locator(".canvas-identity").click()
        expect(page.locator('[title="请输入有效数值"]')).to_have_count(0)
        page.wait_for_timeout(1200)
        expect(page.locator(".save-indicator")).to_contain_text("所有修改已保存")
        expect(page.get_by_role("button", name="重新计算当前凭证")).to_be_enabled()
        expect(page.get_by_role("button", name=re.compile(r"^导出 \d+ 张$"))).to_be_enabled()
        self.assertTrue(any(status == 200 for status, _, _ in self.draft_saves))
        amount_input.click()
        page.locator(".canvas-identity").click()
        expect(page.get_by_text("公式未完成，已保留该单元格原值", exact=True)).to_have_count(0)
        amount_input.click()
        amount_input.fill("=100+20")
        amount_input.press("Enter")
        expect(amount_input).to_have_value("120.00")
        page.locator(".canvas-identity").click()
        expect(page.get_by_text("公式未完成，已保留该单元格原值", exact=True)).to_have_count(0)
        page.wait_for_timeout(1000)
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url.endswith("/recalculate")) as recalculated:
            page.get_by_role("button", name="重新计算当前凭证").click()
        self.assertTrue(recalculated.value.ok)
        expect(page.locator(".paper-status")).to_contain_text("校验通过")
        # Fixing the only exception must keep the active voucher on the review canvas.
        summary_input = page.get_by_label("摘要", exact=True).first
        original_summary = summary_input.input_value()
        summary_input.fill("")
        page.locator(".canvas-identity").click()
        page.wait_for_timeout(1200)
        expect(page.locator(".paper-status")).to_contain_text("待核对")
        expect(page.locator(".save-indicator")).to_contain_text("所有修改已保存")
        page.locator(".list-tabs").get_by_role("button", name=re.compile("^异常")).click()
        page.get_by_role("button", name="专注核对").click()
        summary_input = page.get_by_label("摘要", exact=True).first
        summary_input.fill(original_summary)
        page.locator(".review-header > b").click()
        expect(page.locator(".paper-status")).to_contain_text("校验通过")
        expect(page.locator(".review-save")).to_contain_text("所有修改已保存")
        expect(page.locator(".canvas-identity b")).to_contain_text("02C355")
        page.get_by_role("button", name="退出核对模式").click()
        page.locator(".list-tabs").get_by_role("button", name="全部", exact=True).click()
        zoom_before = page.locator(".zoom-value").inner_text()
        page.get_by_role("button", name="放大凭证").click()
        expect(page.locator(".zoom-value")).not_to_have_text(zoom_before)
        page.get_by_role("button", name="缩小凭证").click()
        expect(page.locator(".zoom-value")).to_have_text(zoom_before)
        page.get_by_label("合同号搜索").fill("NOT-A-CONTRACT")
        expect(page.locator(".voucher-item")).to_have_count(0)
        page.get_by_label("合同号搜索").fill("")
        expect(page.locator(".voucher-item")).to_have_count(1)
        expect(page.locator(".canvas-identity b")).to_contain_text("02C355")
        Path("tmp/browser-audit-desktop.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-audit-desktop.png", full_page=True)

        self.step = "add-row"
        page.get_by_role("button", name="添加分录").click()
        expect(page.locator(".paper-status")).to_contain_text("待核对")
        self.step = "delete-row"
        page.locator(".voucher-grid tbody tr").last.locator('button[title="删除分录"]').click()
        try:
            expect(page.locator(".paper-status")).to_contain_text("校验通过", timeout=10_000)
        except AssertionError as error:
            raise AssertionError(f"{error}\ndraft saves: {self.draft_saves}") from error

        page.get_by_role("button", name="专注核对").click()
        expect(page.locator(".review-header .recalculate-toggle")).to_contain_text("重新计算")
        expect(page.locator(".review-header .recalculate-toggle")).to_contain_text("R")
        Path("tmp/browser-review-recalculate.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-review-recalculate.png", full_page=True)
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url.endswith("/recalculate")):
            page.keyboard.press("r")
        page.keyboard.press("e")
        expect(page.get_by_role("button", name=re.compile("取消异常标记"))).to_be_visible()
        page.keyboard.press("e")
        expect(page.get_by_role("button", name=re.compile("标记为异常"))).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("button", name="专注核对")).to_be_visible()

        mixed_preview = {
            "valid": [
                {"id": "mixed-out", "contracts": ["26MT-MIXED-OUT"], "business_type": "外销",
                 "business_no": "CUSTOMS-MIXED-OUT", "customs_no": "CUSTOMS-MIXED-OUT",
                 "version": 1, "replaces": []},
                {"id": "mixed-in", "contracts": ["26MT-MIXED-IN"], "business_type": "内销",
                 "business_no": "26MT-MIXED-IN", "customs_no": "", "version": 1, "replaces": []},
            ],
            "excluded": [],
        }
        page.route(
            re.compile(r".*/api/exports/preview$"),
            lambda route: route.fulfill(status=200, content_type="application/json",
                                        body=json.dumps(mixed_preview, ensure_ascii=False)),
        )
        page.get_by_role("button", name=re.compile(r"^导出 \d+ 张$")).click()
        export_modal = page.locator(".modal").filter(has_text="导出确认")
        expect(export_modal).to_be_visible()
        page.get_by_label("筛选导出凭证").fill("内销")
        expect(export_modal.locator(".export-choice")).to_have_count(1)
        expect(export_modal.locator(".export-choice")).to_contain_text("26MT-MIXED-IN")
        page.wait_for_timeout(400)
        Path("tmp/browser-export-type-search.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-export-type-search.png", full_page=True)
        page.get_by_label("筛选导出凭证").fill("外销")
        expect(export_modal.locator(".export-choice")).to_have_count(1)
        expect(export_modal.locator(".export-choice")).to_contain_text("26MT-MIXED-OUT")
        expect(export_modal.get_by_role("button", name="全选", exact=True)).to_be_visible()
        expect(export_modal.get_by_role("button", name="清空", exact=True)).to_be_visible()
        expect(export_modal.get_by_role("button", name="取消筛选结果")).to_have_count(0)
        page.get_by_label("筛选导出凭证").fill("内销")
        export_modal.get_by_role("button", name="全选", exact=True).click()
        expect(export_modal.get_by_role("button", name="清空", exact=True)).to_be_enabled()
        export_modal.get_by_role("button", name="清空", exact=True).click()
        page.get_by_role("button", name="关闭导出确认").click()
        page.unroute(re.compile(r".*/api/exports/preview$"))

        page.get_by_role("button", name=re.compile(r"^导出 \d+ 张$")).click()
        expect(page.locator(".modal").filter(has_text="导出确认")).to_be_visible()
        page.get_by_role("button", name="全选", exact=True).click()
        with page.expect_download(timeout=30_000) as download:
            page.get_by_role("button", name=re.compile("导出所选")).click()
        downloaded = download.value
        self.assertTrue(downloaded.suggested_filename.endswith(".xls"))
        self.wait_idle()
        expect(page.get_by_role("heading", name="导出文件已生成")).to_be_visible()
        page.get_by_role("button", name="前往导出记录").click()
        expect(page.locator(".export-table tbody tr")).to_have_count(1)
        expect(page.locator(".export-table tbody tr")).to_contain_text("演示未回写")

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(self.base, wait_until="networkidle")
        expect(page.get_by_text("飞书待开范围", exact=True)).not_to_be_visible()
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 1)
        heading_box = page.locator(".desk-command h1").bounding_box()
        self.assertIsNotNone(heading_box)
        self.assertLess(heading_box["height"], 36)
        Path("tmp/browser-audit-mobile.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-audit-mobile.png", full_page=True)
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.console_errors, [], self.not_found)
        self.assertEqual(self.server_errors, [])

    def test_api_failure_is_visible_without_blank_page(self):
        page = self.page
        page.route("**/api/meta", lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "模拟服务异常"}, ensure_ascii=False),
        ))
        page.goto(self.base, wait_until="networkidle")
        expect(page.locator(".banner.danger")).to_contain_text("模拟服务异常")
        expect(page.get_by_role("heading", name=re.compile("凭证工作台|核对与出具"))).to_be_visible()
        self.assertEqual(self.page_errors, [])

    def test_domestic_customer_mapping_is_visible_and_saved_consistently(self):
        page = self.page
        page.goto(self.base, wait_until="networkidle")
        page.get_by_role("button", name="数据源").click()
        expect(page.get_by_role("heading", name="数据源")).to_be_visible()
        expect(page.locator(".mapping-table tbody tr:not(.group-row)").first).to_be_visible()
        for tab, label in [("产品存货", "产品名称"), ("供应商", "供应商名称"), ("客户", "客户名称")]:
            page.get_by_role("button", name=tab, exact=True).click()
            expect(page.locator(".mapping-table thead th").first).to_have_text(label)
            page.get_by_role("button", name="新增映射", exact=True).click()
            expect(page.get_by_label(label, exact=True)).to_be_visible()
            if tab == "客户":
                expect(page.get_by_role("radio")).to_have_count(2)
                expect(page.get_by_role("radio", name="国内", exact=True)).to_be_checked()
                page.get_by_role("radio", name="国外", exact=True).click()
                expect(page.get_by_role("radio", name="国内", exact=True)).not_to_be_checked()
            page.get_by_role("button", name="关闭映射编辑").click()
            expect(page.get_by_role("button", name="关闭映射编辑")).to_have_count(0)
        page.get_by_label("搜索数据源").fill("上海宝冶冶金工程有限公司")
        expect(page.locator(".group-row", has_text="国内")).to_be_visible()
        row = page.locator(".mapping-table tbody tr", has_text="上海宝冶冶金工程有限公司")
        expect(row).to_be_visible()
        expect(row).to_contain_text("010251")
        expect(row).to_contain_text("上海宝冶集团有限公司")
        Path("tmp/browser-domestic-customers.png").parent.mkdir(exist_ok=True)
        page.screenshot(path="tmp/browser-domestic-customers.png", full_page=True)

        row.get_by_role("button", name="编辑").click()
        modal = page.locator(".modal", has_text="编辑客户映射")
        expect(modal).to_be_visible()
        expect(modal.get_by_role("radio")).to_have_count(2)
        expect(modal.get_by_role("radio", name="国内", exact=True)).to_be_checked()
        modal.get_by_role("radio", name="国外", exact=True).click()
        expect(modal.get_by_role("radio", name="国外", exact=True)).to_be_checked()
        expect(modal.get_by_role("radio", name="国内", exact=True)).not_to_be_checked()
        expect(modal.get_by_label("选择分组")).to_have_count(0)
        page.screenshot(path="tmp/browser-customer-groups.png", full_page=True)
        modal.get_by_role("button", name="保存映射").click()
        expect(modal).not_to_be_visible()
        page.get_by_label("搜索数据源").fill("")
        expect(page.locator(".group-row", has_text="国外")).to_be_visible()
        expect(page.locator(".mapping-table tbody tr", has_text="上海宝冶冶金工程有限公司")).to_be_visible()
        page.get_by_label("搜索数据源").fill("KOCH Engineered")
        expect(page.locator(".group-row", has_text="国内")).to_be_visible()
        expect(page.locator(".mapping-table tbody tr", has_text="KOCH Engineered")).to_be_visible()
        page.get_by_label("搜索数据源").fill("UEX Co., Ltd")
        expect(page.locator(".group-row", has_text="国外")).to_be_visible()
        expect(page.locator(".mapping-table tbody tr", has_text="UEX Co., Ltd")).to_be_visible()
        page.get_by_role("button", name="辅助核算").click()
        page.get_by_label("搜索数据源").fill("2001")
        expect(page.locator(".reference-table .group-row", has_text="2001 · 短期借款")).to_have_count(1)
        expect(page.locator(".reference-table tbody tr:not(.group-row)")).to_have_count(3)
        expect(page.locator(".reference-table")).not_to_contain_text("200101 ·")
        page.get_by_label("搜索数据源").fill("1408")
        expect(page.locator(".reference-table .group-row", has_text="1408 · 委托加工物资")).to_have_count(1)
        expect(page.locator(".reference-table tbody tr:not(.group-row)")).to_have_count(2)
        page.get_by_role("button", name="会计科目").click()
        page.get_by_label("搜索数据源").fill("2001")
        expect(page.locator(".reference-table .group-row", has_text="2001 · 短期借款")).to_have_count(1)
        expect(page.locator(".reference-table tbody tr:not(.group-row)")).to_have_count(3)
        page.get_by_label("搜索数据源").fill("1408")
        expect(page.locator(".reference-table .group-row", has_text="1408 · 委托加工物资")).to_have_count(1)
        expect(page.locator(".reference-table tbody tr:not(.group-row)")).to_have_count(2)

        response = self.context.request.get(self.base + "/api/mappings")
        self.assertTrue(response.ok)
        saved = response.json()
        saved_row = next(row for row in saved["items"]["customers"]
                         if row["alias"] == "上海宝冶冶金工程有限公司")
        self.assertEqual(saved_row["platform_code"], "010251")
        self.assertEqual(saved_row["group_override"], "国外")
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.server_errors, [])


if __name__ == "__main__":
    unittest.main()
