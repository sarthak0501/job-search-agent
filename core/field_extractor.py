from __future__ import annotations
"""
field_extractor.py – Comprehensive form field extraction.

Implements extract_fields(frame) -> list[FieldMeta] with support for:
  - standard inputs/textarea/select
  - radio groups (grouped by name)
  - checkboxes
  - [role=combobox], [role=listbox], [role=option]
  - contenteditable divs
  - telephone inputs (intl-tel-input)
  - date inputs (type=date, split day/month/year selects)
  - file inputs (hidden + visible wrapper)
"""

from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Frame


@dataclass
class FieldMeta:
    # Identity
    id: str = ""
    name: str = ""
    tag: str = ""
    type: str = ""
    role: str = ""

    # Labels (resolved from multiple sources)
    label: str = ""
    placeholder: str = ""
    aria_label: str = ""
    aria_required: bool = False
    disabled: bool = False
    current_value: str = ""

    # Selectors in priority order
    selector_candidates: list = field(default_factory=list)

    # Choice fields
    options: list = field(default_factory=list)  # [{value, label, selected}]
    group_label: str = ""    # for radio/checkbox groups
    section_heading: str = ""  # nearest h2/h3/h4 above

    # Context
    error_text: str = ""
    appears_filled: bool = False

    # Widget classification
    widget_type: str = "standard"  # standard | react_select | custom_listbox | phone_composite | file | date | contenteditable

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "type": self.type,
            "role": self.role,
            "label": self.label,
            "placeholder": self.placeholder,
            "aria_label": self.aria_label,
            "aria_required": self.aria_required,
            "disabled": self.disabled,
            "current_value": self.current_value,
            "selector_candidates": self.selector_candidates,
            "options": self.options,
            "group_label": self.group_label,
            "section_heading": self.section_heading,
            "error_text": self.error_text,
            "appears_filled": self.appears_filled,
            "widget_type": self.widget_type,
        }


# ---------------------------------------------------------------------------
# JS extraction
# ---------------------------------------------------------------------------

_EXTRACT_JS = r"""() => {
    const fields = [];
    const seenRadioNames = new Set();

    function getText(el) {
        return el ? el.innerText.trim().replace(/\s+/g, ' ') : '';
    }

    function getLabel(el) {
        const id = el.id || '';
        // 1. aria-label
        let label = el.getAttribute('aria-label') || '';
        // 2. label[for=id]
        if (!label && id) {
            const lEl = document.querySelector('label[for=' + JSON.stringify(id) + ']');
            if (lEl) label = lEl.innerText.trim();
        }
        // 3. aria-labelledby
        if (!label) {
            const lblby = el.getAttribute('aria-labelledby') || '';
            if (lblby) {
                const parts = lblby.split(/\s+/).map(s => s.trim()).filter(Boolean);
                const texts = parts.map(pid => {
                    const e = document.getElementById(pid);
                    return e ? e.innerText.trim() : '';
                }).filter(Boolean);
                if (texts.length) label = texts.join(' ');
            }
        }
        // 4. Wrapping label
        if (!label) {
            const wrapLabel = el.closest('label');
            if (wrapLabel) {
                // Get the label text excluding the input's own text
                const clone = wrapLabel.cloneNode(true);
                const inp = clone.querySelector('input,select,textarea');
                if (inp) inp.remove();
                label = clone.innerText.trim();
            }
        }
        // 5. Nearest wrapper label/legend
        if (!label) {
            const wrapper = el.closest(
                '.form-group, .field-wrapper, .select__container, fieldset, ' +
                '[class*="question"], [class*="field-container"], [class*="form-field"]'
            );
            if (wrapper) {
                const lEl = wrapper.querySelector('label:not(:has(input)):not(:has(select)):not(:has(textarea)), legend');
                if (lEl) label = lEl.innerText.trim();
            }
        }
        // 6. Nearby sibling label (previous sibling)
        if (!label) {
            let sib = el.previousElementSibling;
            while (sib) {
                if (sib.tagName === 'LABEL' || sib.tagName === 'LEGEND') {
                    label = sib.innerText.trim();
                    break;
                }
                const lEl = sib.querySelector('label, legend');
                if (lEl) {
                    label = lEl.innerText.trim();
                    break;
                }
                sib = sib.previousElementSibling;
                if (!sib || !['div','span','p','li'].includes(sib.tagName.toLowerCase())) break;
            }
        }
        return label.replace(/\s+/g, ' ').substring(0, 200);
    }

    function getGroupLabel(el) {
        const fs = el.closest('fieldset');
        if (fs) {
            const leg = fs.querySelector('legend');
            if (leg) return leg.innerText.trim().substring(0, 200);
        }
        return '';
    }

    function getSectionHeading(el) {
        let node = el.parentElement;
        for (let depth = 0; depth < 15 && node; depth++) {
            const h = node.querySelector('h2,h3,h4');
            if (h) {
                const rect = h.getBoundingClientRect();
                const elRect = el.getBoundingClientRect();
                if (rect.top <= elRect.top) return h.innerText.trim().substring(0, 150);
            }
            node = node.parentElement;
        }
        return '';
    }

    function getErrorText(el) {
        const id = el.id || '';
        // aria-describedby
        const descby = el.getAttribute('aria-describedby') || '';
        if (descby) {
            const parts = descby.split(/\s+/).filter(Boolean);
            for (const pid of parts) {
                const descEl = document.getElementById(pid);
                if (descEl && descEl.innerText.trim()) {
                    const text = descEl.innerText.trim();
                    if (text.length < 300) return text;
                }
            }
        }
        // Nearby error element
        const wrapper = el.closest('.form-group, .field-wrapper, [class*="question"], [class*="field"]');
        if (wrapper) {
            const errEl = wrapper.querySelector(
                '[class*="error"]:not([class*="error-boundary"]), [class*="invalid"], ' +
                '[aria-invalid="true"], [class*="validation-message"], [class*="field-error"]'
            );
            if (errEl && errEl.innerText.trim().length < 300) return errEl.innerText.trim();
        }
        return '';
    }

    function buildSelectorCandidates(el, id, name) {
        const candidates = [];
        if (id) candidates.push('#' + id);
        if (name) candidates.push('[name=' + JSON.stringify(name) + ']');
        if (id) candidates.push('[id=' + JSON.stringify(id) + ']');
        // Label-based selector
        const lbl = getLabel(el);
        if (lbl) candidates.push('[aria-label=' + JSON.stringify(lbl) + ']');
        // Tag + type
        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        if (type) candidates.push(tag + '[type="' + type + '"]');
        return candidates;
    }

    function isVisible(el) {
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function detectWidgetType(el) {
        const id = el.id || '';
        const cls = el.className || '';
        if (id.includes('react-select') || cls.includes('react-select')) return 'react_select';
        if (el.getAttribute('role') === 'combobox') return 'react_select';
        if (el.getAttribute('role') === 'listbox') return 'custom_listbox';
        if (el.getAttribute('contenteditable') === 'true') return 'contenteditable';
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'file') return 'file';
        if (type === 'date' || type === 'month' || type === 'datetime-local') return 'date';
        // intl-tel-input
        if (el.closest('.iti') || cls.includes('iti__') || id.includes('phone')) {
            if (type === 'tel' || type === 'text') return 'phone_composite';
        }
        return 'standard';
    }

    const seen = new Set();
    const allInputs = document.querySelectorAll(
        'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
        'textarea, select, [role="combobox"], [role="textbox"], [contenteditable="true"]'
    );

    allInputs.forEach(el => {
        if (!isVisible(el)) return;

        const id = el.id || '';
        const name = el.getAttribute('name') || '';
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || tag).toLowerCase();

        // Handle radio groups: group all inputs[type=radio][name=X] as ONE logical field
        if (type === 'radio') {
            if (!name || seenRadioNames.has(name)) return;
            seenRadioNames.add(name);

            const radios = Array.from(document.querySelectorAll('input[type=radio][name=' + JSON.stringify(name) + ']'));
            const options = radios.map(r => {
                let optLabel = r.getAttribute('aria-label') || '';
                if (!optLabel) {
                    const lEl = r.id ? document.querySelector('label[for=' + JSON.stringify(r.id) + ']') : null;
                    if (lEl) optLabel = lEl.innerText.trim();
                }
                if (!optLabel) {
                    const wrapper = r.closest('label');
                    if (wrapper) {
                        const clone = wrapper.cloneNode(true);
                        const inp2 = clone.querySelector('input');
                        if (inp2) inp2.remove();
                        optLabel = clone.innerText.trim();
                    }
                }
                return { value: r.value, label: optLabel || r.value, selected: r.checked };
            });

            const firstRadio = radios[0];
            fields.push({
                id: firstRadio.id || '',
                name,
                tag: 'input',
                type: 'radio',
                role: '',
                label: getLabel(firstRadio),
                placeholder: '',
                aria_label: firstRadio.getAttribute('aria-label') || '',
                aria_required: firstRadio.required || firstRadio.getAttribute('aria-required') === 'true',
                disabled: firstRadio.disabled,
                current_value: (radios.find(r => r.checked) || {}).value || '',
                selector_candidates: ['input[type="radio"][name=' + JSON.stringify(name) + ']'],
                options,
                group_label: getGroupLabel(firstRadio),
                section_heading: getSectionHeading(firstRadio),
                error_text: getErrorText(firstRadio),
                appears_filled: radios.some(r => r.checked),
                widget_type: 'standard',
            });
            return;
        }

        // Dedup by id+name
        const key = (id || '') + '||' + (name || '') + '||' + tag + '||' + type;
        if (seen.has(key)) return;
        seen.add(key);

        const role = el.getAttribute('role') || '';
        const required = el.required || el.getAttribute('aria-required') === 'true';
        const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';

        let currentValue = '';
        let appearsFilled = false;
        if (tag === 'select') {
            currentValue = el.options[el.selectedIndex]?.text || el.value || '';
            appearsFilled = !!el.value && el.selectedIndex > 0;
        } else if (type === 'checkbox') {
            currentValue = el.checked ? 'true' : '';
            appearsFilled = el.checked;
        } else if (el.getAttribute('contenteditable') === 'true') {
            currentValue = el.innerText.trim();
            appearsFilled = currentValue.length > 0;
        } else {
            currentValue = el.value || '';
            appearsFilled = currentValue.trim().length > 0;
        }

        let opts = [];
        if (tag === 'select') {
            opts = Array.from(el.options).map(o => ({
                value: o.value,
                label: o.text.trim(),
                selected: o.selected,
            })).filter(o => o.label && o.value !== '');
        } else if (type === 'checkbox') {
            opts = [
                { value: 'true', label: getLabel(el) || 'Checked', selected: el.checked },
                { value: 'false', label: 'Unchecked', selected: !el.checked },
            ];
        }

        fields.push({
            id,
            name,
            tag,
            type,
            role,
            label: getLabel(el),
            placeholder: el.getAttribute('placeholder') || '',
            aria_label: el.getAttribute('aria-label') || '',
            aria_required: required,
            disabled,
            current_value: currentValue,
            selector_candidates: buildSelectorCandidates(el, id, name),
            options: opts,
            group_label: getGroupLabel(el),
            section_heading: getSectionHeading(el),
            error_text: getErrorText(el),
            appears_filled: appearsFilled,
            widget_type: detectWidgetType(el),
        });
    });

    return fields;
}"""


def extract_fields(frame: Frame) -> list[FieldMeta]:
    """
    Extract all visible form fields from the given frame.

    Returns a list of FieldMeta dataclass instances.
    """
    try:
        raw_fields = frame.evaluate(_EXTRACT_JS)
    except Exception as exc:
        print(f"[field_extractor] JS evaluation error: {exc}")
        return []

    result = []
    for f in raw_fields:
        try:
            fm = FieldMeta(
                id=f.get("id", ""),
                name=f.get("name", ""),
                tag=f.get("tag", ""),
                type=f.get("type", ""),
                role=f.get("role", ""),
                label=f.get("label", ""),
                placeholder=f.get("placeholder", ""),
                aria_label=f.get("aria_label", ""),
                aria_required=bool(f.get("aria_required", False)),
                disabled=bool(f.get("disabled", False)),
                current_value=f.get("current_value", ""),
                selector_candidates=f.get("selector_candidates", []),
                options=f.get("options", []),
                group_label=f.get("group_label", ""),
                section_heading=f.get("section_heading", ""),
                error_text=f.get("error_text", ""),
                appears_filled=bool(f.get("appears_filled", False)),
                widget_type=f.get("widget_type", "standard"),
            )
            result.append(fm)
        except Exception as exc:
            print(f"[field_extractor] FieldMeta construction error: {exc}")
            continue

    return result
