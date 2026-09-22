#!/usr/bin/env python3
"""FB4: managed/linked properties, question editing, locale and responsive browser QA."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from playwright.sync_api import sync_playwright, expect
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import (isolated_server, graph_payload, create_project_api, project_editor_url,
    attach_console_guards, assert_no_blocking_console_errors)
from jev_fixtures import node


def open_modal(page):
    """Open via the actual canvas, not detached synthetic HTML."""
    page.locator('.canvas-node[data-node-id="jev-test"] h3').dblclick()
    modal = page.locator('.cw-jev-modal')
    expect(modal).to_be_visible()
    return modal


def main():
    for origin in ("managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            model = install_test_package(server, "jev", origin=origin)
            consumer = node()
            for surface in ("modal", "inspector_panel"):
                rendered = surface_payload(server, model, consumer, surface=surface)
                assert 'data-jev-question-list' in rendered["html"]
            project = create_project_api(server, document=graph_payload("Jev properties", [consumer], []))["project"]
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = attach_console_guards(page)
                page.goto(project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"]))
                modal = open_modal(page)
                expect(modal.locator('[data-block-apply]')).to_be_disabled()
                modal.locator('[data-block-config-field="model"]').fill('jev-1.13.0')
                tabs = modal.locator('[data-jev-tab]')
                tabs.first.press('ArrowRight')
                expect(tabs.nth(1)).to_have_attribute('aria-selected', 'true')
                expect(modal.locator('.jev-question')).to_have_count(3)
                first = modal.locator('.jev-question').first
                first.locator('[data-jev-instructions]').fill('Does the request need code changes?')
                modal.locator('[data-jev-add="choice"]').click()
                expect(modal.locator('.jev-question')).to_have_count(4)
                modal.locator('.jev-question').last.locator('[data-jev-id]').fill('routing')
                modal.locator('.jev-question').last.locator('[data-jev-criteria]').fill('{bad')
                expect(modal.locator('[data-jev-validation]')).to_have_attribute('data-invalid', 'true')
                modal.locator('[data-block-apply]').click()
                expect(modal).to_be_visible()
                modal.locator('.jev-question').last.locator('[data-jev-criteria]').fill('{"agent":"A code task","other":null}')
                expect(modal.locator('[data-jev-validation]')).to_have_attribute('data-invalid', 'false')
                # Structured criteria in the existing score must survive edits elsewhere.
                field = modal.locator('[data-block-config-field="questions"]')
                assert json.loads(field.input_value())['severity']['criteria'][0] == {"meaning": "Cosmetic"}
                for width, height, label in ((1440, 900, 'desktop'), (390, 740, 'mobile'), (320, 568, 'small')):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_function('''() => {
                      const b=document.querySelector('.cw-jev-modal').getBoundingClientRect();
                      return b.left>=-1 && b.right<=innerWidth+1 && b.bottom<=innerHeight+1;
                    }''')
                    bounds = modal.evaluate('''panel => {
                      const b=panel.getBoundingClientRect(), a=panel.querySelector('[data-block-apply]').getBoundingClientRect();
                      return {inside:b.left>=-1 && b.right<=innerWidth+1 && b.bottom<=innerHeight+1,
                        apply:a.bottom<=innerHeight && a.top>=0, overflow:panel.scrollWidth>panel.clientWidth+1,
                        background:getComputedStyle(panel).backgroundColor};
                    }''')
                    modal.locator('.jev-modal-panel:not([hidden])').evaluate('el => el.scrollTop=0')
                    page.screenshot(path=artifact_path(f'jev-{origin}-{label}.png'))
                    assert bounds['inside'] and bounds['apply'] and not bounds['overflow'], bounds
                    assert bounds['background'] not in {'transparent', 'rgba(0, 0, 0, 0)'}, bounds
                page.set_viewport_size({"width": 1440, "height": 900})
                with page.expect_response(lambda r: '/api/blocks/' in r.url and r.url.endswith('/ui-action') and r.request.method == 'POST') as applied:
                    modal.locator('[data-block-apply]').click()
                assert not applied.value.json().get('error'), applied.value.json()
                if modal.is_visible():
                    modal.locator('[data-close-block-modal]').first.click()
                page.reload()
                modal = open_modal(page)
                expect(modal.locator('[data-block-config-field="model"]')).to_have_value('jev-1.13.0')
                modal.locator('[data-jev-tab="questions"]').click()
                expect(modal.locator('.jev-question')).to_have_count(4)
                expect(modal.locator('.jev-question').first.locator('[data-jev-instructions]')).to_have_value('Does the request need code changes?')
                # A malformed advanced editor cannot silently save stale card state.
                modal.locator('.jev-json-details summary').click()
                editor = modal.locator('[data-jev-json]')
                editor.fill('{broken')
                expect(modal.locator('[data-jev-validation]')).to_have_attribute('data-invalid', 'true')
                expect(modal.locator('[data-jev-add="noul"]')).to_be_disabled()
                expect(modal.locator('.jev-question').first.locator('[data-jev-instructions]')).to_be_disabled()
                # Repairing raw JSON restores the builder, retaining structured instructions.
                repaired = {"custom": {"type": "noul", "instructions": {"question": "Does the message need action?"}}}
                editor.fill(json.dumps(repaired))
                expect(modal.locator('[data-jev-add="noul"]')).to_be_enabled()
                expect(modal.locator('.jev-question')).to_have_count(1)
                expect(modal.locator('[data-jev-format]')).to_have_value('json')
                # Cancel really discards this advanced draft, preserving the four saved questions.
                modal.locator('[data-close-block-modal]').first.click()
                page.goto(server.base_url + '/')
                page.locator('#homeApplicationSettingsButton').click()
                page.locator('#applicationLanguageSelect').select_option('fr')
                page.wait_for_function("window.CWMessages.getLanguage() === 'fr'")
                page.goto(project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"]))
                modal = open_modal(page)
                expect(modal.locator('[data-jev-tab="settings"]')).to_have_text('Paramètres')
                page.screenshot(path=artifact_path(f'jev-{origin}-french-settings.png'))
                modal.locator('[data-jev-tab="questions"]').click()
                expect(modal.locator('.jev-question')).to_have_count(4)
                expect(modal.locator('[data-jev-add="noul"]')).to_have_text('+ Oui / non')
                expect(modal.locator('.jev-question').first.locator('[data-jev-remove]')).to_have_text('Supprimer')
                page.screenshot(path=artifact_path(f'jev-{origin}-french-questions.png'))
                assert page.evaluate('!window.CWBlockUiBlocks?.jev')
                assert_no_blocking_console_errors(errors)
            finally:
                browser.close()
        print(f'[ok] Jev {origin} properties', flush=True)


if __name__ == '__main__':
    main()
