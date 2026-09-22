// node --test tests/panel  (no npm; node >= 20)
// The validator must refuse exactly what the clock refuses (dash: DashboardSpecTest, DashboardSpecV6Test), with the same words.
import test from 'node:test';
import assert from 'node:assert/strict';
import * as S from '../../custom_components/helios/panel/helios-schema.js';

const item = (id, type, column, row, width = 1, height = 1, extra = {}) => ({ id, type, column, row, width, height, ...extra });
const tile = (id, entity, column, row, extra = {}) => item(id, 'tile', column, row, 1, 1, { entity, ...extra });
function example() {
  return { pages: [
    { id: 'main', title: 'Dom', items: [
      item('clock', 'clock', 1, 1, 2, 2, { title: 'Dom' }),
      item('weather', 'weather', 3, 1, 2, 1, { entity: 'weather.forecast_dom' }),
      tile('salon', 'light.salon', 3, 2, { icon: 'mdi:ceiling-light', tap_action: { action: 'toggle' } }),
      tile('roleta', 'cover.roleta_salon', 4, 2),
      tile('temp', 'sensor.temperatura_salon', 1, 3, { title: 'Salon' }),
      tile('wilg', 'sensor.czujnik_salon', 2, 3, { attribute: 'humidity', icon: 'mdi:water-percent' }),
      tile('zamek', 'lock.drzwi', 3, 3, { tap_action: { action: 'lock' }, confirmation: { enabled: true, text: 'Zamknąć drzwi?' } }),
      item('music', 'music', 4, 3),
    ] },
    { id: 'sypialnia', title: 'Sypialnia', items: [
      item('rolety', 'cover_group', 1, 1, 1, 1, { covers: [{ entity: 'cover.a', title: 'Roleta A' }, { entity: 'cover.b', title: 'Roleta B' }] }),
      tile('noc', 'scene.noc', 2, 1, { icon: 'mdi:weather-night', title: 'Dobranoc' }),
      item('garaz', 'garage', 1, 3, 4, 1, { entity: 'cover.brama', visible_when: { entity: 'binary_sensor.wieczor', state: 'on' } }),
      item('swiatla', 'entity', 3, 1, 1, 1, { entity: 'sensor.swiatla', icon: 'lightbulb', off_entity: 'light.grupa' }),
    ] },
  ] };
}
const withItem = (id, patch) => { const m = example(); for (const p of m.pages) for (const i of p.items) if (i.id === id) Object.assign(i, patch); return m; };
const msgs = (m) => S.validate(m).map((e) => e.msg);
const rejects = (m, msg) => assert.ok(msgs(m).some((x) => x.startsWith(msg)), `expected "${msg}", got ${JSON.stringify(msgs(m))}`);

test('the example is valid and round-trips as version 6', () => {
  assert.deepEqual(S.validate(example()), []);
  const doc = S.toHelios(example());
  assert.equal(doc.version, 6);
  assert.deepEqual(S.fromLovelace({ helios: doc }).model, example());
  assert.ok(JSON.stringify(doc).length < S.MAX_BYTES);
});
test('legacy documents load as one page and say so', () => {
  const v2 = { version: 2, grid: { columns: 4, rows: 3 }, items: [item('clock', 'clock', 1, 1, 4, 3)] };
  const parsed = S.fromLovelace({ helios: v2, views: [] });
  assert.equal(parsed.legacyVersion, 2);
  assert.deepEqual(parsed.model.pages, [{ id: 'main', items: v2.items }]);
  assert.match(parsed.notice, /wersji 2/);
  assert.equal(S.fromLovelace({}).legacyVersion, null);
  assert.deepEqual(S.fromLovelace({}).model.pages, [{ id: 'main', items: [] }]);
  assert.equal(S.fromLovelace({ helios: { version: 9 } }).model, null);
});
test('geometry and identity rules use the clock wording', () => {
  rejects(withItem('garaz', { width: 5 }), 'Element garaz wychodzi poza siatkę');
  rejects(withItem('roleta', { column: 3 }), 'Element roleta nakłada się na inny element');
  rejects(withItem('roleta', { column: 0 }), 'Pole column musi być liczbą od 1 do 4');
  rejects(withItem('roleta', { id: 'Zła nazwa' }), 'Nieprawidłowy id: Zła nazwa');
  rejects(withItem('noc', { id: 'salon' }), 'Powtórzony id: salon');
  const many = example(); for (let i = 0; i < 9; i++) many.pages[1].items.push(tile('x' + i, 'light.x', 4, 1));
  rejects(many, 'items: najwyżej 12 elementów');
  const pages = example(); for (let i = 0; i < 7; i++) pages.pages.push({ id: 'p' + i, items: [] });
  rejects(pages, 'pages: najwyżej 8 stron');
  rejects({ pages: [{ id: 'main', items: [] }, { id: 'main', items: [] }] }, 'Powtórzony id strony: main');
  rejects({ pages: [{ id: 'Zła', items: [] }] }, 'Nieprawidłowy id strony: Zła');
  const music = example(); music.pages[0].items.push(item('music2', 'music', 4, 2));
  rejects(music, 'Dozwolony jest jeden kafelek music');
  const twoPages = example(); twoPages.pages[1].items.push(item('music2', 'music', 4, 1));
  assert.deepEqual(S.validate(twoPages), []);
  rejects(withItem('temp', { title: 'x'.repeat(41) }), 'Nieprawidłowe pole title');
  assert.deepEqual(S.validate({ pages: [] }), [{ page: null, id: null, msg: 'Wymagane pole pages' }]);
});
test('entities, domains and fields per type', () => {
  rejects(withItem('weather', { entity: 'sensor.pogoda' }), 'Element weather wymaga encji z domeny weather');
  rejects(withItem('garaz', { entity: 'binary_sensor.brama' }), 'Element garaz wymaga encji z domeny cover');
  rejects(withItem('salon', { entity: 'light' }), 'Element salon wymaga encji z domeny tile');
  rejects(withItem('clock', { icon: 'information' }), 'Pole niedozwolone dla typu clock: icon');
  rejects(withItem('salon', { off_entity: 'light.x' }), 'Pole niedozwolone dla typu tile: off_entity');
  rejects(withItem('salon', { icn: 'x' }), 'Pole niedozwolone dla typu tile: icn');
  rejects(withItem('wilg', { attribute: 'Bad-Name' }), 'Nieprawidłowy attribute');
  rejects(withItem('weather', { temperature_entity: 'number.t' }), 'temperature_entity wymaga encji sensor');
  rejects(withItem('weather', { forecast_entity: 'sensor.x' }), 'weather: forecast_entity i forecast_when występują razem');
  rejects(withItem('weather', { forecast_entity: 'sensor.x', forecast_when: { entity: 'binary_sensor.x', state: 'unavailable' } }), 'Brak danych nie może oznaczać trybu prognozy');
  rejects(withItem('swiatla', { off_entity: 'switch.x' }), 'off_entity wymaga encji z domeny light');
  rejects(withItem('rolety', { covers: [{ entity: 'cover.a', title: 'A' }] }), 'cover_group wymaga dokładnie dwóch pozycji covers');
  rejects(withItem('rolety', { covers: [{ entity: 'cover.a', title: 'A' }, { entity: 'cover.a', title: 'B' }] }), 'covers: ta sama roleta dwa razy');
  rejects(withItem('rolety', { covers: [{ entity: 'light.a', title: 'A' }, { entity: 'cover.b', title: 'B' }] }), 'covers wymaga encji z domeny cover');
  rejects(withItem('garaz', { visible_when: { entity: 'binary_sensor.x', state: 'unknown' } }), 'Brak danych nie może oznaczać widoczności');
  rejects(withItem('garaz', { visible_when: { entity: 'binary_sensor.x' } }), 'Wymagane pole state');
  rejects(withItem('temp', { type: 'button' }), 'Nieobsługiwany typ elementu: button');
});
test('intents follow the domain and locks always ask', () => {
  rejects(withItem('temp', { tap_action: { action: 'toggle' } }), 'Encja sensor.temperatura_salon nie obsługuje akcji toggle');
  rejects(withItem('salon', { tap_action: { action: 'open' } }), 'Encja light.salon nie obsługuje akcji open');
  rejects(withItem('salon', { tap_action: { action: 'light.turn_on' } }), 'Encja light.salon nie obsługuje akcji light.turn_on');
  rejects(withItem('salon', { tap_action: { action: 'toggle', data: {} } }), 'Nieznane pole tap_action');
  rejects(withItem('zamek', { confirmation: { enabled: false } }), 'Akcja lock wymaga potwierdzenia');
  assert.deepEqual(S.validate(withItem('temp', { confirmation: { enabled: true } })), []); // the clock accepts it on any tile
  rejects(withItem('temp', { confirmation: { enabled: 'yes' } }), 'confirmation.enabled musi być boolean');
  assert.deepEqual(S.validate(withItem('temp', { tap_action: { action: 'none' } })), []);
  rejects(withItem('garaz', { tap_action: { action: 'open' } }), 'Typ garage dopuszcza wyłącznie tap_action.action: close');
  rejects(withItem('swiatla', { tap_action: { action: 'toggle' } }), 'Typ entity dopuszcza wyłącznie tap_action.action: lights_off');
  assert.deepEqual(S.allowedIntents('lock'), ['none', 'details', 'lock', 'unlock']);
  assert.equal(S.defaultIntent('cover'), 'controls'); assert.equal(S.defaultIntent('script'), 'activate'); assert.equal(S.defaultIntent('sensor'), 'details');
});
test('icons: mdi names or the six legacy words', () => {
  rejects(withItem('salon', { icon: 'alert' }), 'Nieznana ikona: alert');
  rejects(withItem('salon', { icon: 'mdi:Bad Name' }), 'Nieznana ikona: mdi:Bad Name');
  assert.deepEqual(S.validate(withItem('salon', { icon: 'lightbulb' })), []);
  assert.deepEqual(S.validate(withItem('music', { icon: 'music' })), []);
});
test('slug: ascii, unique, bounded', () => {
  assert.equal(S.slug('Łazienka górna'), 'lazienka-gorna');
  assert.equal(S.slug('Łazienka górna', ['lazienka-gorna']), 'lazienka-gorna-2');
  assert.equal(S.slug('Łazienka górna', ['lazienka-gorna', 'lazienka-gorna-2']), 'lazienka-gorna-3');
  assert.equal(S.slug('!!!'), 'karta');
  assert.equal(S.slug('a'.repeat(50)).length, 40);
  assert.equal(S.slug('a'.repeat(50), ['a'.repeat(40)]).length, 40);
});
test('forms fold and unfold the nested wire fields', () => {
  const zamek = example().pages[0].items[6];
  const form = S.toForm(zamek);
  assert.equal(form.action, 'lock'); assert.equal(form.confirm_enabled, true); assert.equal(form.confirm_text, 'Zamknąć drzwi?');
  assert.deepEqual(S.fromForm('tile', form), zamek);
  const roleta = example().pages[0].items[3];
  assert.equal(S.toForm(roleta).action, 'controls');
  assert.deepEqual(S.fromForm('tile', S.toForm(roleta)), roleta); // the default intent is not written back
  assert.deepEqual(S.fromForm('tile', { ...S.toForm(roleta), action: 'none' }).tap_action, { action: 'none' });
  const garaz = example().pages[1].items[2];
  assert.deepEqual(S.fromForm('garage', S.toForm(garaz)), garaz);
  const rolety = example().pages[1].items[0];
  assert.deepEqual(S.fromForm('cover_group', S.toForm(rolety)), rolety);
  const weather = item('w', 'weather', 1, 1, 2, 1, { entity: 'weather.x', forecast_entity: 'sensor.j', forecast_when: { entity: 'binary_sensor.t', state: 'on' } });
  assert.deepEqual(S.fromForm('weather', S.toForm(weather)), weather);
  assert.equal(S.fromForm('tile', { ...S.toForm(roleta), title: '   ' }).title, undefined);
  assert.ok(S.schemaFor('tile', roleta).some((s) => s.name === 'action' && s.selector.select.options.map((o) => o.value).join() === 'none,details,controls,open,close,stop'));
  assert.ok(S.schemaFor('light', garaz, 2).some((s) => s.name === 'icon' && s.selector.select));
  assert.ok(!S.schemaFor('entity', garaz, 4).some((s) => s.name === 'off_entity'));
  assert.ok(S.schemaFor('entity', garaz, 5).some((s) => s.name === 'off_entity'));
});
test('legacy tiles convert to tile with the same behaviour', () => {
  const m = example();
  assert.deepEqual(S.toTile(m.pages[1].items[2]), { id: 'garaz', type: 'tile', column: 1, row: 3, width: 4, height: 1, entity: 'cover.brama', visible_when: { entity: 'binary_sensor.wieczor', state: 'on' }, icon: 'mdi:garage-open', tap_action: { action: 'close' }, confirmation: { enabled: true } });
  assert.equal(S.toTile(m.pages[1].items[3]), null); // entity + off_entity has no tile equivalent
  const light = item('l', 'light', 1, 1, 1, 1, { entity: 'light.x', title: 'L' });
  assert.deepEqual(S.toTile(light), { id: 'l', type: 'tile', column: 1, row: 1, width: 1, height: 1, entity: 'light.x', title: 'L', icon: 'mdi:lightbulb', tap_action: { action: 'toggle' } });
  const converted = example(); converted.pages[1].items[2] = S.toTile(converted.pages[1].items[2]);
  assert.deepEqual(S.validate(converted), []);
});
test('new items and placement', () => {
  const page = example().pages[0];
  assert.equal(S.fits(page.items, { column: 4, row: 2, width: 1, height: 1 }), false);
  assert.equal(S.fits(page.items, { column: 4, row: 2, width: 1, height: 1 }, 'roleta'), true);
  assert.equal(S.fits(page.items, { column: 4, row: 3, width: 2, height: 1 }, 'music'), false);
  const clock = S.emptyItem('clock', { column: 1, row: 1 }, ['zegar']);
  assert.equal(clock.id, 'zegar-2'); assert.equal(clock.width, 2);
  const t = S.emptyItem('tile', { column: 2, row: 2 }, [], 'light.x');
  assert.deepEqual(t, { id: 'kafelek', type: 'tile', column: 2, row: 2, width: 1, height: 1, entity: 'light.x' });
  assert.ok(S.emptyItem('cover_group', { column: 1, row: 1 }, []).covers.length === 2);
});
test('size limit', () => {
  const m = example(); m.pages[0].items[4].title = 'x'.repeat(40);
  for (let n = 0; n < 6; n++) m.pages.push({ id: 'p' + n, items: Array.from({ length: 12 }, (_, i) => tile('t' + n + '-' + i, 'sensor.' + 'a'.repeat(600), (i % 4) + 1, Math.floor(i / 4) + 1, { attribute: 'a'.repeat(64), visible_when: { entity: 'binary_sensor.' + 'b'.repeat(600), state: 'x'.repeat(60) } })) });
  rejects(m, 'Sekcja helios przekracza 64 KiB');
});
