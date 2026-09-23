// Helios dashboard model for the editor panel: pure, DOM-free, testable with `node --test`.
// Mirrors the clock's parser (dash: app/src/main/java/pl/mateusz/helios/DashboardSpec.java, ActionPolicy.java).
// Every message here is the clock's own wording, so what the editor refuses is exactly what the clock would refuse.

export const VERSION = 6;
export const COLUMNS = 4, ROWS = 3, MAX_ITEMS = 12, MAX_PAGES = 8, MAX_BYTES = 65536;
export const ID_RE = /^[a-z0-9_-]{1,40}$/, ENTITY_RE = /^[a-z0-9_]+\.[a-z0-9_]+$/, ATTRIBUTE_RE = /^[a-z0-9_]{1,64}$/, MDI_RE = /^mdi:[a-z0-9-]{1,40}$/;
export const LEGACY_ICONS = ['information', 'weather-rainy', 'lightbulb', 'window-shutter', 'garage-open', 'music'];

// type -> {label, fields (beyond id/type/geometry/title/visible_when), entityDomain ('' any, null none), legacy}
export const TYPES = {
  clock: { label: 'Zegar', fields: [], entityDomain: null },
  weather: { label: 'Pogoda', fields: ['entity', 'temperature_entity', 'forecast_entity', 'forecast_when'], entityDomain: 'weather' },
  tile: { label: 'Kafelek (dowolna encja)', fields: ['entity', 'icon', 'attribute', 'tap_action', 'confirmation'], entityDomain: '' },
  music: { label: 'Muzyka', fields: ['icon'], entityDomain: null, singleton: true },
  cover_group: { label: 'Dwie rolety', fields: ['covers', 'icon'], entityDomain: null },
  entity: { label: 'Encja (wersja 2-5)', fields: ['entity', 'attribute', 'icon', 'off_entity', 'tap_action', 'confirmation'], entityDomain: '', legacy: true },
  light: { label: 'Światło (wersja 2-5)', fields: ['entity', 'icon', 'tap_action', 'confirmation'], entityDomain: 'light', legacy: true, action: 'toggle' },
  cover: { label: 'Roleta (wersja 2-5)', fields: ['entity', 'icon', 'tap_action', 'confirmation'], entityDomain: 'cover', legacy: true, action: 'controls' },
  garage: { label: 'Brama (wersja 2-5)', fields: ['entity', 'icon', 'tap_action', 'confirmation'], entityDomain: 'cover', legacy: true, action: 'close' },
};
export const DOMAIN_ICON = { light: 'mdi:lightbulb', switch: 'mdi:toggle-switch', input_boolean: 'mdi:toggle-switch', fan: 'mdi:fan', cover: 'mdi:window-shutter', lock: 'mdi:lock', sensor: 'mdi:eye', binary_sensor: 'mdi:radiobox-blank', climate: 'mdi:thermostat', script: 'mdi:script-text', scene: 'mdi:palette', input_button: 'mdi:gesture-tap-button', button: 'mdi:gesture-tap-button', media_player: 'mdi:cast', weather: 'mdi:weather-partly-cloudy' };
export const LEGACY_DEFAULT_ICON = { entity: 'information', light: 'lightbulb', cover: 'window-shutter', garage: 'garage-open', music: 'music', cover_group: 'window-shutter' };

// --- intents (ActionPolicy) ---
const SWITCH_DOMAINS = ['light', 'switch', 'input_boolean', 'fan'], RUN_DOMAINS = ['script', 'scene', 'input_button', 'button'];
export const INTENT_LABEL = { none: 'Nic', details: 'Szczegóły', toggle: 'Przełącz', turn_on: 'Włącz', turn_off: 'Wyłącz', controls: 'Panel rolety', open: 'Otwórz', close: 'Zamknij', stop: 'Zatrzymaj', activate: 'Uruchom', lock: 'Zamknij zamek', unlock: 'Otwórz zamek' };
export function domainOf(entity) { return typeof entity === 'string' && entity.includes('.') ? entity.slice(0, entity.indexOf('.')) : ''; }
export function allowedIntents(domain) {
  if (SWITCH_DOMAINS.includes(domain)) return ['none', 'details', 'toggle', 'turn_on', 'turn_off'];
  if (domain === 'cover') return ['none', 'details', 'controls', 'open', 'close', 'stop'];
  if (domain === 'lock') return ['none', 'details', 'lock', 'unlock'];
  if (RUN_DOMAINS.includes(domain)) return ['none', 'details', 'activate'];
  return ['none', 'details'];
}
export function defaultIntent(domain) { return domain === 'cover' ? 'controls' : RUN_DOMAINS.includes(domain) ? 'activate' : 'details'; }
export const FORCED_CONFIRM = ['lock', 'unlock'];

// --- ids ---
export function slug(text, taken = []) {
  let s = String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[łŁ]/g, 'l').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/g, '');
  if (!s) s = 'karta';
  let out = s, n = 2;
  while (taken.includes(out)) { const suffix = '-' + n++; out = s.slice(0, 40 - suffix.length) + suffix; }
  return out;
}

/** SPEC 0.16: a free Lovelace url_path for a clock's own dashboard - "helios-" + slug of its name, -2, -3... when taken. */
export function dashboardPath(name, taken = []) {
  const base = 'helios-' + slug(name, []).replace(/^helios(-|$)/, '').replace(/^$/, 'zegar');
  let out = base, n = 2;
  while (taken.includes(out)) out = `${base}-${n++}`;
  return out;
}

// --- document <-> model ---
// model = {pages: [{id, title?, items: [helios item objects]}]}; items are the wire objects themselves.
export function fromLovelace(doc) {
  const helios = doc && typeof doc === 'object' ? doc.helios : undefined;
  if (!helios || typeof helios !== 'object') return { model: { pages: [{ id: 'main', items: [] }] }, legacyVersion: null, notice: 'Ten pulpit nie ma jeszcze sekcji helios - zapis ją utworzy.' };
  const v = helios.version;
  if (Number.isInteger(v) && v >= 2 && v <= 5) {
    const items = Array.isArray(helios.items) ? helios.items.map(clone) : [];
    return { model: { pages: [{ id: 'main', items }] }, legacyVersion: v, notice: `Dokument w wersji ${v}. Zapis przekształci go do wersji 6 (strony). Wymaga Heliosa 0.12; starszy zegar odrzuci dokument i zachowa poprzedni układ.` };
  }
  if (v === VERSION) {
    const pages = Array.isArray(helios.pages) ? helios.pages.map((p) => ({ id: p.id, ...(p.title ? { title: p.title } : {}), items: Array.isArray(p.items) ? p.items.map(clone) : [] })) : [];
    return { model: { pages: pages.length ? pages : [{ id: 'main', items: [] }] }, legacyVersion: null, notice: null };
  }
  return { model: null, legacyVersion: null, notice: `Nieznana wersja sekcji helios (${v}) - edytuj ręcznie.` };
}
export function toHelios(model) {
  return { version: VERSION, pages: model.pages.map((p) => ({ id: p.id, ...(p.title ? { title: p.title } : {}), items: p.items.map(clone) })) };
}
export function clone(o) { return JSON.parse(JSON.stringify(o)); }

// --- new items ---
export function emptyItem(type, cell, taken, entity) {
  const item = { id: slug(TYPES[type].label.split(' ')[0], taken), type, column: cell.column, row: cell.row, width: 1, height: 1 };
  if (type === 'clock') { item.width = 2; item.height = 2; item.title = 'Dom'; }
  if (type === 'weather') { item.width = 2; }
  if (type === 'cover_group') item.covers = [{ entity: '', title: 'Roleta A' }, { entity: '', title: 'Roleta B' }];
  if (TYPES[type].entityDomain !== null) item.entity = entity || '';
  return item;
}
/** Legacy light/cover/garage/entity as a tile with the same behaviour; entity+off_entity has no tile equivalent and stays. */
export function toTile(item) {
  if (item.type === 'entity' && item.off_entity) return null;
  const tile = { id: item.id, type: 'tile', column: item.column, row: item.row, width: item.width, height: item.height, entity: item.entity };
  if (item.title) tile.title = item.title;
  if (item.attribute) tile.attribute = item.attribute;
  if (item.visible_when) tile.visible_when = clone(item.visible_when);
  const icon = item.icon || LEGACY_DEFAULT_ICON[item.type];
  if (icon) tile.icon = icon.startsWith('mdi:') ? icon : 'mdi:' + icon;
  const action = { light: 'toggle', cover: 'controls', garage: 'close', entity: 'none' }[item.type];
  tile.tap_action = { action };
  if (item.type === 'garage') tile.confirmation = item.confirmation ? clone(item.confirmation) : { enabled: true };
  else if (item.confirmation) tile.confirmation = clone(item.confirmation);
  return tile;
}

// --- geometry ---
export function occupied(items, ignoreId) {
  const used = [];
  for (const i of items) { if (i.id === ignoreId) continue; for (let r = i.row; r < i.row + i.height; r++) for (let c = i.column; c < i.column + i.width; c++) used.push(`${c},${r}`); }
  return new Set(used);
}
export function fits(items, candidate, ignoreId) {
  if (candidate.column < 1 || candidate.row < 1 || candidate.column + candidate.width - 1 > COLUMNS || candidate.row + candidate.height - 1 > ROWS) return false;
  const used = occupied(items, ignoreId);
  for (let r = candidate.row; r < candidate.row + candidate.height; r++) for (let c = candidate.column; c < candidate.column + candidate.width; c++) if (used.has(`${c},${r}`)) return false;
  return true;
}

// --- validation (DashboardSpec.java wording) ---
export function validate(model) {
  const errors = [], push = (page, id, msg) => errors.push({ page, id, msg });
  if (!model || !Array.isArray(model.pages) || model.pages.length === 0) return [{ page: null, id: null, msg: 'Wymagane pole pages' }];
  if (model.pages.length > MAX_PAGES) push(null, null, `pages: najwyżej ${MAX_PAGES} stron`);
  const pageIds = new Set(), ids = new Set();
  model.pages.forEach((p, pi) => {
    if (typeof p.id !== 'string' || !ID_RE.test(p.id)) push(pi, null, `Nieprawidłowy id strony: ${p.id}`);
    else if (pageIds.has(p.id)) push(pi, null, `Powtórzony id strony: ${p.id}`); else pageIds.add(p.id);
    if (p.title != null && (typeof p.title !== 'string' || !p.title.trim() || p.title.length > 40)) push(pi, null, 'Nieprawidłowe pole title');
    const items = Array.isArray(p.items) ? p.items : [];
    if (items.length > MAX_ITEMS) push(pi, null, `items: najwyżej ${MAX_ITEMS} elementów`);
    const used = new Set(); let music = 0;
    for (const i of items) {
      const e = (msg) => push(pi, i.id, msg);
      const def = TYPES[i.type];
      if (!def) { e(`Nieobsługiwany typ elementu: ${i.type}`); continue; }
      if (typeof i.id !== 'string' || !ID_RE.test(i.id)) e(`Nieprawidłowy id: ${i.id}`);
      else if (ids.has(i.id)) e(`Powtórzony id: ${i.id}`); else ids.add(i.id);
      if (def.singleton && ++music > 1) e(`Dozwolony jest jeden kafelek ${i.type}`);
      for (const k of ['column', 'row', 'width', 'height']) if (!Number.isInteger(i[k]) || i[k] < 1 || i[k] > (k === 'row' || k === 'height' ? ROWS : COLUMNS)) e(`Pole ${k} musi być liczbą od 1 do ${k === 'row' || k === 'height' ? ROWS : COLUMNS}`);
      if (Number.isInteger(i.column) && Number.isInteger(i.width) && Number.isInteger(i.row) && Number.isInteger(i.height)) {
        if (i.column + i.width - 1 > COLUMNS || i.row + i.height - 1 > ROWS) e(`Element ${i.id} wychodzi poza siatkę`);
        else for (let r = i.row; r < i.row + i.height; r++) for (let c = i.column; c < i.column + i.width; c++) { const key = `${c},${r}`; if (used.has(key)) { e(`Element ${i.id} nakłada się na inny element`); r = ROWS + 1; break; } used.add(key); }
      }
      if (i.title != null && (typeof i.title !== 'string' || !i.title.trim() || i.title.length > 40)) e('Nieprawidłowe pole title');
      const domainLabel = def.entityDomain === '' ? i.type : def.entityDomain;
      if (def.entityDomain !== null) {
        if (typeof i.entity !== 'string' || !ENTITY_RE.test(i.entity) || (def.entityDomain && !i.entity.startsWith(def.entityDomain + '.'))) e(`Element ${i.id} wymaga encji z domeny ${domainLabel}`);
      } else if (i.entity != null) e(`Pole niedozwolone dla typu ${i.type}: entity`);
      for (const k of Object.keys(i)) if (!['id', 'type', 'column', 'row', 'width', 'height', 'title', 'visible_when', ...def.fields].includes(k)) e(`Pole niedozwolone dla typu ${i.type}: ${k}`);
      if (i.temperature_entity != null && !/^sensor\.[a-z0-9_]+$/.test(i.temperature_entity)) e('temperature_entity wymaga encji sensor');
      if (i.attribute != null && !ATTRIBUTE_RE.test(i.attribute)) e('Nieprawidłowy attribute');
      if (i.off_entity != null && !/^light\.[a-z0-9_]+$/.test(i.off_entity)) e('off_entity wymaga encji z domeny light');
      if ((i.forecast_entity != null) !== (i.forecast_when != null)) e('weather: forecast_entity i forecast_when występują razem');
      if (i.forecast_entity != null && !/^sensor\.[a-z0-9_]+$/.test(i.forecast_entity)) e('forecast_entity wymaga encji sensor');
      if (i.forecast_when != null) when(i.forecast_when, 'forecast_when', e);
      if (i.visible_when != null) when(i.visible_when, 'visible_when', e);
      if (i.type === 'cover_group') {
        const covers = Array.isArray(i.covers) ? i.covers : [];
        if (covers.length !== 2) e('cover_group wymaga dokładnie dwóch pozycji covers');
        else {
          for (const c of covers) { if (!/^cover\.[a-z0-9_]+$/.test(c.entity || '')) e('covers wymaga encji z domeny cover'); if (typeof c.title !== 'string' || !c.title.trim() || c.title.length > 40) e('Nieprawidłowe pole title'); }
          if (covers[0].entity && covers[0].entity === covers[1].entity) e('covers: ta sama roleta dwa razy');
        }
      }
      if (i.icon != null) {
        if (typeof i.icon !== 'string' || !(MDI_RE.test(i.icon) || LEGACY_ICONS.includes(i.icon))) e(`Nieznana ikona: ${i.icon}`);
      }
      let intent = null;
      if (i.type === 'tile') {
        const domain = domainOf(i.entity);
        intent = i.tap_action ? i.tap_action.action : defaultIntent(domain);
        if (i.tap_action && (typeof i.tap_action !== 'object' || Object.keys(i.tap_action).some((k) => k !== 'action'))) e('Nieznane pole tap_action');
        if (!allowedIntents(domain).includes(intent)) e(`Encja ${i.entity} nie obsługuje akcji ${intent}`);
      } else if (i.tap_action != null) {
        const action = def.action || (i.type === 'entity' && i.off_entity ? 'lights_off' : null);
        if (!action) e(`Pole niedozwolone dla typu ${i.type}: tap_action`);
        else if (i.tap_action.action !== action) e(`Typ ${i.type} dopuszcza wyłącznie tap_action.action: ${action}`);
      }
      if (i.confirmation != null) {
        const tappable = i.type === 'tile' || !!(def.action || (i.type === 'entity' && i.off_entity)); // the clock takes confirmation on any tile, even a display-only one
        if (!tappable) e(`Pole niedozwolone dla typu ${i.type}: confirmation`);
        if (typeof i.confirmation.enabled !== 'boolean') e('confirmation.enabled musi być boolean');
        if (i.confirmation.text != null && (typeof i.confirmation.text !== 'string' || !i.confirmation.text.trim() || i.confirmation.text.length > 80)) e('Nieprawidłowe pole text');
        if (FORCED_CONFIRM.includes(intent) && i.confirmation.enabled === false) e(`Akcja ${intent} wymaga potwierdzenia`);
      }
    }
  });
  if (JSON.stringify(toHelios(model)).length > MAX_BYTES) push(null, null, 'Sekcja helios przekracza 64 KiB');
  return errors;
}
function when(w, key, e) {
  if (typeof w !== 'object' || w === null) { e(`${key} musi być obiektem`); return; }
  if (typeof w.entity !== 'string' || !ENTITY_RE.test(w.entity)) e(`Nieprawidłowa encja ${key}`);
  if (typeof w.state !== 'string' || !w.state.trim()) e('Wymagane pole state');
  else if (w.state === 'unknown' || w.state === 'unavailable') e(`Brak danych nie może oznaczać ${key === 'visible_when' ? 'widoczności' : 'trybu prognozy'}`);
}

// --- forms (ha-form data is flat; nested wire fields are folded and unfolded here) ---
export function toForm(item) {
  const f = { ...item };
  delete f.visible_when; delete f.confirmation; delete f.tap_action; delete f.covers; delete f.forecast_when;
  if (item.visible_when) { f.visible_entity = item.visible_when.entity; f.visible_state = item.visible_when.state; }
  if (item.forecast_when) { f.forecast_when_entity = item.forecast_when.entity; f.forecast_when_state = item.forecast_when.state; }
  if (item.confirmation) { f.confirm_enabled = item.confirmation.enabled; if (item.confirmation.text) f.confirm_text = item.confirmation.text; }
  if (item.type === 'tile') f.action = item.tap_action ? item.tap_action.action : defaultIntent(domainOf(item.entity));
  if (item.covers) { f.cover1_entity = item.covers[0]?.entity; f.cover1_title = item.covers[0]?.title; f.cover2_entity = item.covers[1]?.entity; f.cover2_title = item.covers[1]?.title; }
  return f;
}
export function fromForm(type, data) {
  const def = TYPES[type], item = { id: data.id, type, column: data.column, row: data.row, width: data.width, height: data.height };
  const text = (k) => (typeof data[k] === 'string' && data[k].trim() ? data[k].trim() : undefined);
  if (text('title')) item.title = text('title');
  for (const k of ['entity', 'temperature_entity', 'forecast_entity', 'attribute', 'off_entity', 'icon']) if (def.fields.includes(k) && text(k)) item[k] = text(k);
  if (def.fields.includes('forecast_when') && (text('forecast_when_entity') || text('forecast_when_state'))) item.forecast_when = { entity: text('forecast_when_entity') || '', state: text('forecast_when_state') || '' };
  if (text('visible_entity') || text('visible_state')) item.visible_when = { entity: text('visible_entity') || '', state: text('visible_state') || '' };
  if (type === 'tile') { const action = data.action || defaultIntent(domainOf(item.entity)); if (action !== defaultIntent(domainOf(item.entity)) || data.action === 'none') item.tap_action = { action }; }
  else if (def.action && data.tap_action_set) item.tap_action = { action: def.action };
  if (def.fields.includes('confirmation') && typeof data.confirm_enabled === 'boolean') { item.confirmation = { enabled: data.confirm_enabled }; if (text('confirm_text')) item.confirmation.text = text('confirm_text'); }
  if (def.fields.includes('covers')) item.covers = [{ entity: text('cover1_entity') || '', title: text('cover1_title') || '' }, { entity: text('cover2_entity') || '', title: text('cover2_title') || '' }];
  return item;
}
/** ha-form schema per type; `item` decides the intent options (domain) and `version` the icon selector. */
export function schemaFor(type, item, legacyVersion) {
  const def = TYPES[type], s = [];
  s.push({ name: 'id', required: true, selector: { text: {} } });
  s.push({ name: 'title', selector: { text: {} } });
  s.push({ type: 'grid', name: '', schema: [
    { name: 'column', required: true, selector: { number: { min: 1, max: COLUMNS, mode: 'box' } } },
    { name: 'row', required: true, selector: { number: { min: 1, max: ROWS, mode: 'box' } } },
    { name: 'width', required: true, selector: { number: { min: 1, max: COLUMNS, mode: 'box' } } },
    { name: 'height', required: true, selector: { number: { min: 1, max: ROWS, mode: 'box' } } },
  ] });
  if (def.entityDomain !== null) s.push({ name: 'entity', required: true, selector: { entity: def.entityDomain ? { domain: def.entityDomain } : {} } });
  if (def.fields.includes('temperature_entity')) s.push({ name: 'temperature_entity', selector: { entity: { domain: 'sensor' } } });
  if (def.fields.includes('forecast_entity')) {
    s.push({ name: 'forecast_entity', selector: { entity: { domain: 'sensor' } } });
    s.push({ name: 'forecast_when_entity', selector: { entity: {} } });
    s.push({ name: 'forecast_when_state', selector: { text: {} } });
  }
  if (def.fields.includes('attribute')) s.push({ name: 'attribute', selector: { text: {} } });
  if (def.fields.includes('off_entity') && (legacyVersion == null || legacyVersion >= 5)) s.push({ name: 'off_entity', selector: { entity: { domain: 'light' } } });
  if (def.fields.includes('icon')) s.push(def.legacy || type === 'music' || type === 'cover_group'
    ? { name: 'icon', selector: { select: { mode: 'dropdown', options: LEGACY_ICONS.map((v) => ({ value: v, label: v })) } } }
    : { name: 'icon', selector: { icon: {} } });
  if (type === 'tile') s.push({ name: 'action', selector: { select: { mode: 'dropdown', options: allowedIntents(domainOf(item.entity)).map((v) => ({ value: v, label: INTENT_LABEL[v] })) } } });
  if (def.fields.includes('confirmation')) { s.push({ name: 'confirm_enabled', selector: { boolean: {} } }); s.push({ name: 'confirm_text', selector: { text: {} } }); }
  if (def.fields.includes('covers')) for (const n of [1, 2]) { s.push({ name: `cover${n}_entity`, required: true, selector: { entity: { domain: 'cover' } } }); s.push({ name: `cover${n}_title`, required: true, selector: { text: {} } }); }
  s.push({ name: 'visible_entity', selector: { entity: {} } });
  s.push({ name: 'visible_state', selector: { text: {} } });
  return s;
}
export const LABELS = { id: 'Identyfikator', title: 'Tytuł', column: 'Kolumna', row: 'Wiersz', width: 'Szerokość', height: 'Wysokość', entity: 'Encja', temperature_entity: 'Czujnik temperatury', forecast_entity: 'Prognoza na jutro (sensor)', forecast_when_entity: 'Tryb prognozy: encja', forecast_when_state: 'Tryb prognozy: stan', attribute: 'Atrybut zamiast stanu', off_entity: 'Światła do zgaszenia (light)', icon: 'Ikona', action: 'Dotknięcie', confirm_enabled: 'Pytaj przed wykonaniem', confirm_text: 'Treść pytania', cover1_entity: 'Roleta A: encja', cover1_title: 'Roleta A: nazwa', cover2_entity: 'Roleta B: encja', cover2_title: 'Roleta B: nazwa', visible_entity: 'Widoczny, gdy encja', visible_state: 'ma stan' };
