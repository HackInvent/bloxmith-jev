"""Autonomous Jev properties and public runtime hooks."""

from html import escape
import json

from bloxsmith_app.block_api import BlockDefinition, BlockRuntimePreparation, render_inspector_template, render_node_card_template
from .runtime import execute, normalize_config, JevError


# FB1: One typed state input, one JSON result, independent questions batched together.
# FB2: Wallet-only credentials, strict questions/answers, sanitized failures and limits.
# FB3: Identical centralized/active execution, cancellation and no partial publication.
# FB4: Release-local accessible properties, English/French and responsive layouts.
class JevBlock(BlockDefinition):
    """Evaluate authored questions without adding framework-specific behavior."""

    kind = "jev"

    def prepare_runtime(self, context):
        """Validate configuration without resolving secrets or contacting TypeSafe."""
        normalize_config(context.config, self.model)
        return BlockRuntimePreparation()

    def execute_runtime(self, context):
        """Use the same adapter in both supported execution modes."""
        return execute(context, self.model)

    def text(self, key, fallback):
        """Render block-owned translatable labels, never authored data as markup."""
        full = "block.jev." + key
        return f'<span data-i18n="{full}">{escape(self.translate(full, fallback=fallback))}</span>'

    def field(self, config, key, title, *, kind="text", attrs=""):
        """Expose accessible fields to the generic draft/Apply binding."""
        numeric = 'data-block-value-type="integer"' if kind == "number" else ""
        return (f'<label class="field-group">{self.text(key, title)}<input type="{kind}" {attrs} {numeric} '
                f'data-block-config-field="{key}" value="{escape(str(config.get(key, "")), quote=True)}"></label>')

    def settings(self, node):
        """Prioritize authentication/model, disclose retry and input limits progressively."""
        config = {**self.default_config(), **(node.get("config") or {})}
        choices = ''.join(f'<option value="{escape(v, quote=True)}"></option>' for v in self.model["model_options"])
        return (
            '<section class="jev-modal-section">'
            f'<h3>{self.text("connection", "Connection")}</h3>'
            f'<label class="field-group">{self.text("title", "Block name")}<input data-block-title-field value="{escape(str(node.get("title") or self.default_title()), quote=True)}"></label>'
            f'{self.field(config, "api_key_ref", "TypeSafe wallet reference", attrs="placeholder=\"secret://workspace/typesafe_api\" autocomplete=\"off\" spellcheck=\"false\"")}'
            f'<p class="field-hint">{self.text("key_hint", "Store your TypeSafe API key in the wallet. Enter only its reference here, never the key.")}</p>'
            f'{self.field(config, "model", "Model", attrs="list=\"jev-model-options\" spellcheck=\"false\"")}<datalist id="jev-model-options">{choices}</datalist>'
            f'<p class="field-hint">{self.text("model_hint", "jev-latest follows stable releases. Enter a versioned Jev ID to pin the model.")}</p>'
            '</section><section class="jev-modal-section jev-summary">'
            f'<h3>{self.text("flow", "Input → evaluation → result")}</h3>'
            f'<p>{self.text("flow_hint", "Connect text or JSON to state. All questions run in one request. result contains the model, typed answers, probabilities and token usage.")}</p>'
            f'<p class="field-hint">{self.text("privacy", "Each execution sends the state and questions to TypeSafe, including in One Shot Simulation. Jev judges data; it does not generate prose or execute code.")}</p>'
            '</section><details class="jev-modal-section"><summary>'
            f'{self.text("limits", "Limits and retries")}</summary><div class="jev-advanced">'
            f'{self.field(config, "timeout_sec", "Total timeout (s)", kind="number", attrs="min=1 max=120 step=1")}'
            f'{self.field(config, "max_state_chars", "State limit (characters)", kind="number", attrs="min=1 max=250000 step=1000")}'
            f'{self.field(config, "max_retries", "Retries on rate limit / overload", kind="number", attrs="min=0 max=3 step=1")}'
            f'<p class="field-hint">{self.text("limits_hint", "Retries apply only to HTTP 429/529, within the total timeout. Network failures are not retried. Provider token limits still apply.")}</p>'
            '</div></details>')

    def questions(self, node):
        """Provide a question builder and a lossless JSON editor for structured guidance."""
        config = {**self.default_config(), **(node.get("config") or {})}
        return (
            '<section class="jev-modal-section">'
            f'<h3>{self.text("questions", "Questions")}</h3>'
            f'<p class="field-hint">{self.text("questions_hint", "Ask one clear judgment per question. Questions are independent: they cannot see each other’s answers. Their IDs are for your workflow, not instructions to Jev.")}</p>'
            f'<input type="hidden" data-block-config-field="questions" value="{escape(config["questions"] if isinstance(config["questions"], str) else json.dumps(config["questions"]), quote=True)}">'
            '<div class="jev-question-tools">'
            f'<button type="button" class="ghost-btn" data-jev-add="noul">{self.text("add_noul", "+ Yes / no")}</button>'
            f'<button type="button" class="ghost-btn" data-jev-add="choice">{self.text("add_choice", "+ Choice")}</button>'
            f'<button type="button" class="ghost-btn" data-jev-add="score">{self.text("add_score", "+ Score")}</button>'
            '</div><div data-jev-question-list></div>'
            '<p class="jev-validation" data-jev-validation role="status" aria-live="polite"></p>'
            f'<details class="jev-json-details"><summary>{self.text("json_editor", "Advanced: edit all questions as JSON")}</summary>'
            f'<label class="field-group">{self.text("questions_json", "Questions JSON")}<textarea data-jev-json spellcheck="false" rows="16"></textarea></label>'
            f'<p class="field-hint">{self.text("json_hint", "Structured instructions and criteria (objects or arrays) are supported. Changes here update the question cards when you leave the editor.")}</p></details>'
            '</section><template data-jev-question-template><section class="jev-question">'
            '<div class="jev-question-heading"><label class="field-group">'
            f'{self.text("question_id", "Identifier")}<input data-jev-id maxlength="128" spellcheck="false"></label>'
            f'<span class="jev-type" data-jev-type></span><button type="button" class="ghost-btn" data-jev-remove>{self.text("remove", "Remove")}</button></div>'
            f'<label class="field-group">{self.text("instructions", "Question / instructions")}<textarea data-jev-instructions rows="3"></textarea></label>'
            f'<label class="field-group jev-encoding">{self.text("instruction_format", "Instruction format")}<select data-jev-format><option value="text" data-i18n="block.jev.text_format">Text</option><option value="json">JSON</option></select></label>'
            f'<label class="field-group">{self.text("criteria", "Criteria (JSON)")}<textarea data-jev-criteria spellcheck="false" rows="5"></textarea></label>'
            '<p class="field-hint" data-jev-criteria-hint></p></section></template>')

    def render_modal(self, *, node, payload=None):
        """Keep settings and questions separate, with reachable fixed footer actions."""
        tabs, panels = [], []
        for index, (key, title, content) in enumerate((("settings", "Settings", self.settings(node)), ("questions", "Questions", self.questions(node)))):
            tabs.append(f'<button type="button" class="jev-modal-tab" data-jev-tab="{key}" id="jev-tab-{key}" role="tab" aria-controls="jev-panel-{key}" aria-selected="{str(index == 0).lower()}" tabindex="{0 if index == 0 else -1}">{self.text(key, title)}</button>')
            panels.append(f'<section class="jev-modal-panel" data-jev-panel="{key}" id="jev-panel-{key}" role="tabpanel" aria-labelledby="jev-tab-{key}"{" hidden" if index else ""}>{content}</section>')
        html = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        for key, value in {"node_id": escape(str(node.get("id", "")), quote=True), "node_title": escape(str(node.get("title") or self.default_title())), "tabs": ''.join(tabs), "panels": ''.join(panels)}.items():
            html = html.replace("{{ " + key + " }}", value)
        return {"html": html, "context": {"node_id": node.get("id"), "node_kind": self.kind}}

    def render_inspector_panel(self, *, node, payload=None):
        """Reuse the same editors and generic inspector draft/Apply contract."""
        html = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = html.replace("{{ settings }}", self.settings(node)).replace("{{ questions }}", self.questions(node))
        return {"html": render_inspector_template(template=html, node=node, payload=payload), "context": {"node_id": node.get("id"), "full_panel": True}}

    def render_node_card(self, *, node, payload=None):
        """Show configured model without placing input data or secrets on the canvas."""
        config = {**self.default_config(), **(node.get("config") or {})}
        return render_node_card_template(block=self, node=node, node_classes=["jev-node"], replacements={"title": node.get("title") or self.default_title(), "model": config["model"]})

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Reject invalid drafts before changing the blueprint configuration."""
        result = super().handle_ui_action(node=node, action=action, values=values, payload=payload)
        patch = result.get("node_patch") or {}
        if "config" in patch:
            try:
                original = {**(node.get("config") or {}), **patch["config"]}
                clean = normalize_config(original, self.model)
                # Inhibition belongs to the framework, not the API request.
                if "execution" in original:
                    clean["execution"] = original["execution"]
                patch["config"] = clean
            except JevError as error:
                return {"error": str(error)}
        return result
