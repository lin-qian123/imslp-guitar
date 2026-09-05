const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const enginePath = path.join(__dirname, '../public_site/assets/search.js');
const categories = [
  {id: 0, name: 'For guitar', name_zh: '1把吉他·原作', family: 'pure', kind: 'original'},
  {id: 1, name: 'For flute, guitar (arr)', name_zh: '长笛、吉他·改编', family: 'woodwinds', kind: 'arrangement'},
];
const work = (id, title_en, title_zh, composer_en, composer_zh, category_ids = [0]) => ({id, title_en, title_zh, composer_en, composer_zh, category_ids});
const data = {categories, works: [
  work('1', 'Recuerdos de la Alhambra', '《阿尔罕布拉宫的回忆》', 'Tárrega, Francisco', '弗朗西斯科·泰雷加'),
  work('2', 'The Seasons, Op.37a', '《四季，Op.37a》', 'Tchaikovsky, Pyotr', '彼得·柴可夫斯基', [0, 1]),
  work('3', 'Nocturne, Op.9 No.2', '《夜曲，Op.9之2》', 'Chopin, Frédéric', '弗雷德里克·肖邦', [1]),
  work('4', 'Fantaisie, Op.7', '《幻想曲，Op.7》', 'Sor, Fernando', '费尔南多·索尔'),
  work('5', 'Prelude', '《前奏曲》', 'Sor, Carlos', '卡洛斯·索尔'),
  work('6', 'Prelude', '《前奏曲》', 'Bach, Johann Sebastian', '约翰·塞巴斯蒂安·巴赫'),
  work('7', 'Nocturne, Op.9 No.3', '《夜曲，Op.9之3》', 'Chopin, Frédéric', '弗雷德里克·肖邦', [1]),
]};
const aliases = {composers: {
  'Tárrega, Francisco': ['塔雷加', '塔瑞加'],
  'Tchaikovsky, Pyotr': ['柴科夫斯基', 'Tschaikowsky'],
  'Chopin, Frédéric': ['萧邦'],
  'Sor, Fernando': ['费尔南多·梭尔'],
  'Bach, Johann Sebastian': ['巴哈'],
}, works: {'1': ['阿尔汉布拉宫的回忆', '阿尔罕布拉宫的追忆']}};
function createIndex() {
  assert.ok(fs.existsSync(enginePath), 'A browser/Node search engine must support aliases and typo recovery');
  return require(enginePath).createIndex(data, aliases);
}
const ids = response => response.matches.map(match => match.item.id);

test('composer alternate translation finds canonical works without changing source names', () => {
  const response = createIndex().search('塔雷加');
  assert.deepEqual(ids(response), ['1']);
  assert.equal(response.matches[0].matchType, 'alias');
  assert.equal(response.matches[0].item.composer_zh, '弗朗西斯科·泰雷加');
});
test('composer aliases are scoped to full identity, not a shared surname', () => {
  assert.deepEqual(ids(createIndex().search('费尔南多 梭尔')), ['4']);
});
test('an exact composer alias ranks before titles that only mention that name', () => {
  const extra = work('8', 'Variations on a theme by Tarrega', '《塔瑞加主题变奏曲》', 'Other, Author', '另一位作曲家');
  const index = require(enginePath).createIndex({...data, works: [...data.works, extra]}, aliases);
  assert.equal(index.search('塔瑞加').matches[0].item.id, '1');
});
test('work aliases are scoped by work ID', () => {
  assert.deepEqual(ids(createIndex().search('阿尔汉布拉宫的回忆')), ['1']);
});
test('common traditional characters and alternate romanization are searchable', () => {
  assert.deepEqual(ids(createIndex().search('蕭邦 夜曲')), ['3', '7']);
  assert.deepEqual(ids(createIndex().search('Tschaikowsky')), ['2']);
});
test('accent, punctuation and opus spacing variations retain exact numbers', () => {
  assert.deepEqual(ids(createIndex().search('CHOPIN op9 no2')), ['3']);
  assert.deepEqual(ids(createIndex().search('tarrega recuerdos')), ['1']);
  assert.deepEqual(ids(createIndex().search('Chopin op9 no4')), []);
});
test('missing letters and adjacent swapped letters find nearby composer names', () => {
  for (const query of ['Tchaikovky', 'Tchaikvosky', 'Chpo in']) {
    // A split misspelling is deliberately not guessed by dropping a word.
    if (query === 'Chpo in') assert.deepEqual(ids(createIndex().search(query)), []);
    else {
      const response = createIndex().search(query);
      assert.deepEqual(ids(response), ['2']);
      assert.equal(response.mode, 'fuzzy');
    }
  }
  assert.deepEqual(ids(createIndex().search('B cah')), []);
  assert.deepEqual(ids(createIndex().search('Bcah')), ['6']);
});
test('approximate Chinese title matches retain all search terms', () => {
  assert.deepEqual(ids(createIndex().search('阿尔罕布拉宫的回意')), ['1']);
  assert.deepEqual(ids(createIndex().search('柴可夫斯机 四季')), ['2']);
  assert.deepEqual(ids(createIndex().search('柴可夫斯机 夜曲')), []);
});
test('exact hits stay first and do not pull in unrelated fuzzy hits', () => {
  const response = createIndex().search('Sor');
  assert.deepEqual(new Set(ids(response)), new Set(['4', '5']));
  assert.equal(response.mode, 'exact');
});
test('short and numeric queries are never loosely corrected', () => {
  for (const query of ['99', 'zz', '曲子', '索拉']) assert.deepEqual(ids(createIndex().search(query)), []);
});
test('original/arrangement/family filters must match one category membership', () => {
  const index = createIndex();
  assert.deepEqual(ids(index.search('Tchaikovky', {family:'woodwinds', kind:'original'})), []);
  const response = index.search('Tchaikovky', {family:'woodwinds', kind:'arrangement'});
  assert.deepEqual(ids(response), ['2']);
  assert.deepEqual(response.matches[0].categories.map(c => c.id), [1]);
  assert.deepEqual(ids(index.search('Tchaikovky', {category:'0'})), ['2']);
});
test('empty query still lists the filtered catalog and does not recommend random corrections', () => {
  const index = createIndex();
  assert.equal(index.search('').matches.length, 7);
  assert.deepEqual(index.suggest(''), []);
});
test('recommendations include canonical composers for aliases and misspellings', () => {
  const index = createIndex();
  for (const query of ['塔雷', '塔瑞加', 'Tarrega', 'Tarega']) {
    const suggestions = index.suggest(query);
    assert.ok(suggestions.some(s => s.kind === 'composer' && s.query === 'Tárrega, Francisco'), query);
    assert.ok(suggestions.length <= 6);
    assert.equal(new Set(suggestions.map(s => s.query)).size, suggestions.length);
  }
});
test('title recommendations preserve the composer in the selected query', () => {
  const suggestions = createIndex().suggest('阿尔罕');
  assert.ok(suggestions.some(s => s.kind === 'work' && s.query.includes('Recuerdos de la Alhambra') && s.query.includes('Tárrega')));
});
test('recommendations respect filters rather than suggesting inaccessible matches', () => {
  assert.deepEqual(createIndex().suggest('塔雷加', {family:'woodwinds'}), []);
});

test('the landing view shows categories until a query or exact category is selected', () => {
  const engine = require(enginePath);
  assert.equal(typeof engine.catalogView, 'function');
  assert.equal(engine.catalogView(''), 'categories');
  assert.equal(engine.catalogView('  ', {family:'woodwinds', kind:'arrangement'}), 'categories');
  assert.equal(engine.catalogView('塔瑞加'), 'works');
  assert.equal(engine.catalogView('', {category:'0'}), 'works');
});

test('category browsing keeps source labels, separates types and counts unique works', () => {
  const index = createIndex();
  assert.equal(typeof index.browse, 'function');
  const all = index.browse();
  assert.deepEqual(all.categories.map(category => category.name), ['For guitar', 'For flute, guitar (arr)']);
  assert.equal(all.workCount, 7);
  const mixed = index.browse({family:'woodwinds',kind:'arrangement'});
  assert.deepEqual(mixed.categories.map(category => category.id), [1]);
  assert.equal(mixed.workCount, 3);
  assert.deepEqual(index.browse({family:'woodwinds',kind:'original'}), {categories:[], workCount:0});
});

test('every approved category is reachable through the landing directory', () => {
  const catalog = require('../public_site/data/catalog.json');
  const index = require(enginePath).createIndex(catalog);
  assert.equal(typeof index.browse, 'function');
  const directory = index.browse();
  assert.equal(directory.categories.length, catalog.summary.category_count);
  assert.equal(directory.workCount, catalog.summary.unique_work_count);
  assert.equal(new Set(directory.categories.map(category => category.id)).size, catalog.categories.length);
  const pureOriginal = index.browse({family:'pure',kind:'original'}).categories;
  assert.equal(pureOriginal[0].name, 'For guitar');
  assert.ok(pureOriginal.findIndex(c => c.name === 'For 2 guitars') < pureOriginal.findIndex(c => c.name === 'For 12 guitars'));
});

test('versioned aliases resolve to real canonical identities and work IDs', () => {
  const aliasPath = path.join(__dirname, '../public_site/data/search-aliases.json');
  assert.ok(fs.existsSync(aliasPath), 'Curated alternate search names must be versioned');
  const aliases = JSON.parse(fs.readFileSync(aliasPath, 'utf8'));
  const catalog = require('../public_site/data/catalog.json');
  const names = new Set(catalog.works.map(work => work.composer_en));
  const workIds = new Set(catalog.works.map(work => work.id));
  for (const [name, values] of Object.entries(aliases.composers)) {
    assert.ok(names.has(name), name);
    assert.ok(values.length > 0 && values.every(value => typeof value === 'string' && value.trim()));
    assert.equal(new Set(values).size, values.length);
  }
  for (const [id, values] of Object.entries(aliases.works)) {
    assert.ok(workIds.has(id), id);
    assert.ok(values.length > 0 && values.every(value => typeof value === 'string' && value.trim()));
  }
  const index = require(enginePath).createIndex(catalog, aliases);
  assert.ok(ids(index.search('塔雷加 阿尔汉布拉')).includes('33377'));
  assert.ok(ids(index.search('德布西 月光')).includes('2397'));
  assert.ok(ids(index.search('Moonlight Sonata')).includes('1458'));
  assert.equal(index.search('Tchaikovky').matches.filter(m => m.item.composer_en === 'Tchaikovsky, Pyotr').length, 8);
});
