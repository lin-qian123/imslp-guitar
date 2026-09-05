"use strict";

// Shared by the browser and Node regression tests. No network or DOM access.
const GuitarSearch = (() => {
  // Search folding only: source names and reviewed display titles stay intact.
  // Deliberately limited to common music/name characters, not a translation engine.
  const variants = Object.fromEntries([
    '蕭萧','爾尔','羅罗','馬马','亞亚','維维','納纳','貝贝','魯鲁','茲兹','裡里',
    '裏里','葉叶','華华','薩萨','蘇苏','喬乔','奧奥','費费','德德','達达','漢汉',
    '謝谢','馮冯','賽赛','寧宁','賈贾','倫伦','萊莱','門门','蘭兰','諾诺','齊齐',
    '樂乐','鋼钢','練练','習习','變变','圓圆','詠咏','嘆叹','調调',
    '諧谐','謔谑','夢梦','愛爱','憶忆','淚泪','敘叙','獻献','給给','詩诗','聲声',
    '長长','風风','與与','豎竖','簫箫','號号','單单','雙双','協协',
    '麗丽','蓮莲','聖圣','誕诞','莊庄','國国','歐欧','鄉乡','謠谣',
    '來来','歸归','別别','離离','歡欢','鳥鸟','鵝鹅','鵑鹃','鶴鹤','飛飞','龍龙',
    '鄧邓','慶庆','後后','黃黄','紅红','綠绿','藍蓝','銀银','滿满','無无','為为',
    '從从','這这','幾几','歲岁','時时','間间','廣广','場场','邊边','遠远',
    '選选','節节','組组','編编','絃弦','絲丝','紡纺','織织','線线','終终','續续',
  ].map(pair => [...pair]));

  function normalize(value) {
    return String(value || '').normalize("NFKD").replace(/\p{Diacritic}/gu, '')
      .toLowerCase().replace(/./gu, char => variants[char] || char)
      .replace(/[æœøłß]/g, char => ({æ:'ae', œ:'oe', ø:'o', ł:'l', ß:'ss'})[char])
      .replace(/([\p{L}])(\d)/gu, '$1 $2').replace(/(\d)([\p{L}])/gu, '$1 $2')
      .replace(/[^\p{L}\p{N}]+/gu, ' ').trim().replace(/\s+/g, ' ');
  }

  function fieldSet(values) {
    const fields = values.map(normalize).filter(Boolean);
    const blob = fields.join(' ');
    const tokens = new Set(blob.match(/[a-z]+|\p{Script=Han}+|\d+/gu) || []);
    return {fields, blob, tokens};
  }

  function contains(fields, term) {
    // Op.2 must not accidentally match Op.27 or a fragment of a work ID.
    return /^\d+$/.test(term) ? fields.tokens.has(term) : fields.blob.includes(term);
  }

  function exactScore(fields, query, terms) {
    if (!terms.every(term => contains(fields, term))) return null;
    if (!query) return 4;
    if (fields.fields.includes(query)) return 0;
    if (fields.fields.some(field => field.startsWith(query))) return 1;
    if (fields.fields.some(field => terms.every(term => contains(fieldSet([field]), term)))) return 2;
    return 3;
  }

  function tolerance(term) {
    if (/^[a-z]{4,40}$/.test(term)) return term.length >= 8 ? 2 : 1;
    if (/^\p{Script=Han}{3,24}$/u.test(term)) return term.length >= 7 ? 2 : 1;
    return 0;
  }

  // Optimal-string-alignment distance, including adjacent transpositions.
  // Chinese titles are unsegmented, so compare against substrings of a Han run.
  function distance(query, word, limit, substring) {
    if (!substring && Math.abs(query.length - word.length) > limit) return limit + 1;
    let previous = Array.from({length: word.length + 1}, (_, j) => substring ? 0 : j);
    let beforePrevious;
    for (let i = 1; i <= query.length; i++) {
      const row = [i];
      let minimum = i;
      for (let j = 1; j <= word.length; j++) {
        row[j] = Math.min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (query[i - 1] !== word[j - 1]));
        if (i > 1 && j > 1 && query[i - 1] === word[j - 2] && query[i - 2] === word[j - 1]) {
          row[j] = Math.min(row[j], beforePrevious[j - 2] + 1);
        }
        minimum = Math.min(minimum, row[j]);
      }
      if (minimum > limit) return limit + 1;
      beforePrevious = previous;
      previous = row;
    }
    return substring ? Math.min(...previous) : previous[word.length];
  }

  function tokenCost(term, token, limit) {
    const han = /^\p{Script=Han}+$/u.test(term);
    if (han !== /^\p{Script=Han}+$/u.test(token)) return limit + 1;
    if (!han && Math.abs(term.length - token.length) > limit) return limit + 1;
    if (han && token.length < term.length - limit) return limit + 1;
    // Cheap rejection before allocating edit-distance rows.
    let absent = 0;
    for (const char of term) if (!token.includes(char)) absent++;
    if (absent > limit) return limit + 1;
    return distance(term, token, limit, han);
  }

  function passes(category, filters) {
    return (!filters.family || filters.family === 'all' || category.family === filters.family)
      && (!filters.kind || filters.kind === 'all' || category.kind === filters.kind)
      && (filters.category === undefined || filters.category === 'all' || String(category.id) === String(filters.category));
  }

  function catalogView(input, filters = {}) {
    return !normalize(input) && (filters.category === undefined || filters.category === 'all') ? 'categories' : 'works';
  }

  function createIndex(data, aliases = {}) {
    const byCategory = new Map(data.categories.map(category => [category.id, category]));
    const vocabulary = new Map();
    const composers = new Map();
    const documents = data.works.map((item, index) => {
      const categories = item.category_ids.map(id => byCategory.get(id));
      const composerAliases = aliases.composers?.[item.composer_en] || [];
      const workAliases = aliases.works?.[item.id] || [];
      const primary = [item.title_en, item.title_zh, item.composer_en, item.composer_zh];
      const source = fieldSet([...primary, item.id, ...categories.flatMap(c => [c.name, c.name_zh])]);
      const expanded = fieldSet([...source.fields, ...composerAliases, ...workAliases]);
      for (const token of expanded.tokens) {
        if (!vocabulary.has(token)) vocabulary.set(token, []);
        vocabulary.get(token).push(index);
      }
      if (!composers.has(item.composer_en)) composers.set(item.composer_en, {
        fields: fieldSet([item.composer_en, item.composer_zh, ...composerAliases]),
        label: item.composer_zh, detail: item.composer_en, query: item.composer_en, kind: 'composer',
      });
      return {item, categories, source, expanded};
    });
    let lastKey;
    let lastResponse;

    function search(input, filters = {}) {
      const query = normalize(input);
      const key = JSON.stringify([query, filters.family, filters.kind, filters.category]);
      if (key === lastKey) return lastResponse;
      const terms = query.split(' ').filter(Boolean);
      const eligible = documents.map(doc => doc.categories.filter(category => passes(category, filters)));
      let matches = [];
      documents.forEach((doc, index) => {
        if (!eligible[index].length) return;
        const direct = exactScore(doc.source, query, terms);
        const expanded = exactScore(doc.expanded, query, terms);
        if (direct !== null || expanded !== null) matches.push({
          item: doc.item, categories: eligible[index], score: Math.min(direct ?? Infinity, expanded === null ? Infinity : 0.5 + expanded),
          matchType: direct !== null ? 'exact' : 'alias',
        });
      });
      let mode = 'exact';
      // Never dilute exact/alias hits. Only use fuzzy recovery for a genuine miss.
      if (!matches.length && terms.length && terms.length <= 12 && query.length <= 160) {
        const costs = terms.map(term => {
          const found = new Map();
          documents.forEach((doc, index) => {
            if (eligible[index].length && contains(doc.expanded, term)) found.set(index, 0);
          });
          const limit = tolerance(term);
          if (limit) for (const [token, indices] of vocabulary) {
            const cost = tokenCost(term, token, limit);
            if (cost <= limit) for (const index of indices) {
              if (eligible[index].length && (!found.has(index) || cost < found.get(index))) found.set(index, cost);
            }
          }
          return found;
        });
        const smallest = [...costs].sort((a, b) => a.size - b.size)[0];
        for (const [index] of smallest) {
          if (!costs.every(cost => cost.has(index))) continue;
          const total = costs.reduce((sum, cost) => sum + cost.get(index), 0);
          if (total > 2) continue;
          matches.push({item: documents[index].item, categories: eligible[index], score: total, matchType: 'fuzzy'});
        }
        if (matches.length) mode = 'fuzzy';
      }
      matches.sort((a, b) => a.score - b.score || a.item.composer_en.localeCompare(b.item.composer_en)
        || a.item.title_en.localeCompare(b.item.title_en) || a.item.id.localeCompare(b.item.id));
      lastKey = key;
      lastResponse = {matches, mode};
      return lastResponse;
    }

    function suggest(input, filters = {}) {
      const query = normalize(input);
      if (query.length < 2) return [];
      const terms = query.split(' ').filter(Boolean);
      const response = search(input, filters);
      const suggestions = [];
      const seen = new Set();
      for (const match of response.matches) {
        const composer = composers.get(match.item.composer_en);
        if (seen.has(composer.query)) continue;
        seen.add(composer.query);
        const relevant = terms.every(term => contains(composer.fields, term)
          || (response.mode === 'fuzzy' && tolerance(term) > 0
            && [...composer.fields.tokens].some(token => tokenCost(term, token, tolerance(term)) <= tolerance(term))));
        if (relevant) {
          suggestions.push({label: composer.label, detail: composer.detail, query: composer.query, kind: 'composer'});
          if (suggestions.length === 3) break;
        }
      }
      for (const match of response.matches) {
        if (suggestions.length === 6) break;
        const item = match.item;
        const query = `${item.title_en} ${item.composer_en}`;
        if (seen.has(query)) continue;
        seen.add(query);
        suggestions.push({label: item.title_zh.replace(/^《|》$/g, ''), detail: item.composer_zh, query, kind: 'work'});
      }
      return suggestions;
    }
    function browse(filters = {}) {
      const order = new Intl.Collator('en', {numeric:true, sensitivity:'base'});
      const nameKey = category => category.name.replace(/^For guitar(?= |$)/, 'For 1 guitar');
      const categories = data.categories.filter(category => passes(category, filters)).sort((a, b) =>
        Number(a.kind === 'arrangement') - Number(b.kind === 'arrangement') || order.compare(nameKey(a), nameKey(b)));
      const ids = new Set(categories.map(category => category.id));
      const workCount = documents.filter(doc => doc.item.category_ids.some(id => ids.has(id))).length;
      return {categories, workCount};
    }
    return {search, suggest, browse};
  }
  return {normalize, createIndex, catalogView};
})();

if (typeof module !== 'undefined' && module.exports) module.exports = GuitarSearch;
