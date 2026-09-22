import { withProperties } from "./properties.js";

/** Release-local question builder. All API calls and credentials remain server-side. */
function mountOwned(root) {
  const controller = new AbortController();
  const { signal } = controller;
  const field = root.querySelector('[data-block-config-field="questions"]');
  const list = root.querySelector('[data-jev-question-list]');
  const editor = root.querySelector('[data-jev-json]');
  const feedback = root.querySelector('[data-jev-validation]');
  if (!field || !list || !editor) return { dispose: () => controller.abort() };
  let valid = true;
  const label = (element, key, fallback) => {
    if (window.CWI18n?.set) window.CWI18n.set(element, `block.jev.${key}`, {}, fallback);
    else element.textContent = fallback;
  };
  const plain = (element, value) => {
    if (window.CWI18n?.setText) window.CWI18n.setText(element, value);
    else element.textContent = value;
  };
  const examples = {
    noul: { type: 'noul', instructions: 'Does the message ask someone to perform an action?' },
    choice: { type: 'choice', instructions: 'Which team should handle this request?', criteria: {
      technical: 'Software, code or technical problems', research: 'Information gathering and source verification',
      other: 'No listed team fits, or there is not enough information' } },
    score: { type: 'score', instructions: 'How severe is the reported issue?', criteria: [
      'Cosmetic issue with no functional impact', 'Degraded functionality with a workaround', 'Blocking issue with no workaround' ] }
  };
  const hints = {
    noul: ['noul_hint', 'Optional: {"true":"What counts as yes","false":"What counts as no"}. The answer is P(yes), not intensity.'],
    choice: ['choice_hint', 'Object with 2–255 named options and descriptions (or null). Include other when no option may fit.'],
    score: ['score_hint', 'Array of 2–10 concrete, ordered levels. The score runs from 0 to the last level’s index.']
  };

  /** Switch tabs without discarding drafts and support the keyboard tab pattern. */
  function activate(tab, focus = false) {
    for (const item of root.querySelectorAll('[data-jev-tab]')) {
      item.setAttribute('aria-selected', String(item === tab)); item.tabIndex = item === tab ? 0 : -1;
    }
    for (const panel of root.querySelectorAll('[data-jev-panel]')) panel.hidden = panel.dataset.jevPanel !== tab.dataset.jevTab;
    if (focus) tab.focus();
  }

  /** Report invalid drafts locally; never allow Apply to save an older valid snapshot. */
  function persist(questions, error = false) {
    valid = !error;
    field.value = JSON.stringify(error ? null : questions);
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
    label(feedback, error ? 'invalid_questions' : 'questions_valid', error
      ? 'Check the JSON, unique identifiers and required instructions. At least one question is required.'
      : 'Draft ready. Apply to save these questions.');
    feedback.dataset.invalid = String(error);
  }

  /** Render arbitrary authored values with DOM value/text APIs, never innerHTML. */
  function render(questions) {
    list.replaceChildren();
    if (!questions || Array.isArray(questions) || typeof questions !== 'object') throw new Error('questions');
    for (const [id, question] of Object.entries(questions)) {
      if (!question || !hints[question.type]) throw new Error('type');
      const card = root.querySelector('[data-jev-question-template]').content.firstElementChild.cloneNode(true);
      card.dataset.type = question.type;
      card.querySelector('[data-jev-id]').value = id;
      plain(card.querySelector('[data-jev-type]'), question.type);
      const structured = typeof question.instructions !== 'string';
      card.querySelector('[data-jev-format]').value = structured ? 'json' : 'text';
      card.querySelector('[data-jev-instructions]').value = structured ? JSON.stringify(question.instructions, null, 2) : question.instructions;
      card.querySelector('[data-jev-criteria]').value = 'criteria' in question ? JSON.stringify(question.criteria, null, 2) : '';
      list.append(card);
      // The clone needs its release ancestor before resolving its own catalog.
      for (const element of card.querySelectorAll('[data-i18n]')) {
        window.CWI18n?.set(element, element.dataset.i18n, {}, element.textContent);
      }
      label(card.querySelector('[data-jev-criteria-hint]'), ...hints[question.type]);
    }
  }

  /** Collect cards losslessly, preserving JSON instructions and option descriptions. */
  function collect() {
    const questions = Object.create(null);
    for (const card of list.children) {
      const id = card.querySelector('[data-jev-id]').value.trim();
      if (!id || Object.hasOwn(questions, id)) throw new Error('identifier');
      const text = card.querySelector('[data-jev-instructions]').value;
      const instructions = card.querySelector('[data-jev-format]').value === 'json' ? JSON.parse(text) : text;
      if (!structured(instructions)) throw new Error('instructions');
      const question = { type: card.dataset.type, instructions };
      const criteria = card.querySelector('[data-jev-criteria]').value.trim();
      if (criteria) question.criteria = JSON.parse(criteria);
      validateQuestion(question);
      questions[id] = question;
    }
    if (!Object.keys(questions).length || Object.keys(questions).length > 64) throw new Error('count');
    return questions;
  }

  /** Match the common server validation rules for immediate draft feedback. */
  function structured(value) {
    return typeof value === 'string' ? !!value.trim() : value !== null && typeof value === 'object' && Object.keys(value).length > 0;
  }

  function validateQuestion(question) {
    if (!question || !structured(question.instructions) || Object.keys(question).some(k => !['type', 'instructions', 'criteria'].includes(k))) throw new Error('question');
    const criteria = question.criteria;
    if (question.type === 'noul') {
      if ('criteria' in question && (!criteria || Array.isArray(criteria) || typeof criteria !== 'object' || Object.keys(criteria).sort().join(',') !== 'false,true' || !Object.values(criteria).every(structured))) throw new Error('criteria');
    } else if (question.type === 'choice') {
      if (!criteria || Array.isArray(criteria) || typeof criteria !== 'object' || Object.keys(criteria).length < 2 || Object.keys(criteria).length > 255 || !Object.keys(criteria).every(k => k.trim()) || !Object.values(criteria).every(v => v === null || structured(v))) throw new Error('criteria');
    } else if (question.type === 'score') {
      if (!Array.isArray(criteria) || criteria.length < 2 || criteria.length > 10 || !criteria.every(structured)) throw new Error('criteria');
    } else throw new Error('type');
  }

  function changedCards() {
    try {
      const questions = collect();
      editor.value = JSON.stringify(questions, null, 2); persist(questions);
    } catch { persist(null, true); }
  }

  /** An invalid raw draft must not be overwritten by edits to stale visible cards. */
  function lockCards(locked) {
    for (const control of list.querySelectorAll('input, textarea, select, button')) control.disabled = locked;
    for (const button of root.querySelectorAll('[data-jev-add]')) button.disabled = locked;
  }

  try {
    const questions = JSON.parse(field.value);
    editor.value = JSON.stringify(questions, null, 2); render(questions);
  } catch {
    editor.value = field.value; valid = false;
    label(feedback, 'invalid_questions', 'Check the JSON, unique identifiers and required instructions. At least one question is required.');
  }
  root.addEventListener('input', event => {
    if (list.contains(event.target)) changedCards();
    if (event.target === editor) {
      // Retain the exact authored JSON for the server's duplicate-key validation.
      field.value = editor.value;
      field.dispatchEvent(new Event('input', { bubbles: true }));
      try {
        const value = JSON.parse(editor.value);
        valid = !!value && !Array.isArray(value) && typeof value === 'object' && Object.keys(value).length > 0 && Object.keys(value).length <= 64;
        if (valid) {
          for (const question of Object.values(value)) validateQuestion(question);
          render(value);
        }
      } catch { valid = false; }
      lockCards(!valid);
      label(feedback, valid ? 'questions_valid' : 'invalid_questions', valid
        ? 'Draft ready. Apply to save these questions.' : 'Check the JSON, unique identifiers and required instructions. At least one question is required.');
      feedback.dataset.invalid = String(!valid);
    }
  }, { signal });
  root.addEventListener('change', event => {
    if (event.target.matches('[data-jev-format]')) changedCards();
    if (event.target === editor && valid) {
      try { render(JSON.parse(editor.value)); } catch { valid = false; }
    }
  }, { signal });
  root.addEventListener('click', event => {
    const tab = event.target.closest('[data-jev-tab]');
    if (tab) activate(tab, true);
    const add = event.target.closest('[data-jev-add]');
    if (add) {
      try {
        const questions = list.children.length ? collect() : {};
        let index = 1;
        while (Object.hasOwn(questions, `question_${index}`)) index++;
        questions[`question_${index}`] = structuredClone(examples[add.dataset.jevAdd]);
        render(questions); changedCards();
        list.lastElementChild.querySelector('[data-jev-id]').focus();
      } catch { persist(null, true); }
    }
    const remove = event.target.closest('[data-jev-remove]');
    if (remove) { remove.closest('.jev-question').remove(); changedCards(); }
  }, { signal });
  root.addEventListener('click', event => {
    if (event.target.closest('[data-block-apply]') && !valid) {
      event.preventDefault(); event.stopImmediatePropagation();
      const tab = root.querySelector('[data-jev-tab="questions"]');
      if (tab) activate(tab, true);
      feedback.scrollIntoView({ block: 'nearest' });
    }
  }, { signal, capture: true });
  root.addEventListener('keydown', event => {
    const tab = event.target.closest('[data-jev-tab]');
    if (!tab) return;
    const tabs = [...root.querySelectorAll('[data-jev-tab]')], index = tabs.indexOf(tab);
    const next = { ArrowRight: (index + 1) % tabs.length, ArrowDown: (index + 1) % tabs.length,
      ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowUp: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
    if (next !== undefined) { event.preventDefault(); activate(tabs[next], true); }
  }, { signal });
  return { dispose: () => controller.abort() };
}

/** Keep the block behavior and add properties-only accessibility. */
export function mount(root, ...args) {
  return withProperties(mountOwned).call(this, root, ...args);
}
