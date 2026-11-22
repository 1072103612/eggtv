const fs = require('fs');
const vm = require('vm');
const path = require('path');
(async () => {
  const root = path.resolve(__dirname, '..');
  const settings = JSON.parse(fs.readFileSync(path.join(root, 'eggtv_sync.json')));
  const base = settings.repo.raw_base.replace(/\/$/, '') + '/';
  const config = JSON.parse(fs.readFileSync(path.join(root, 'tvbox_config.json')));
  const url = config.sites.find(s => s.type === 3 && s.api.endsWith('/drpy2.min.js')).api;
  const createContext = () => vm.createContext({console, URL, TextEncoder, TextDecoder, print: () => {}, setTimeout, clearTimeout, btoa, atob,
    joinUrl: (base, relative) => new URL(relative, base).href});
  let context = createContext();
  const cache = new Map();
  const file = url => path.join(root, decodeURIComponent(url.slice(base.length)));
  async function load(url) {
    if (cache.has(url)) return cache.get(url);
    if (!url.startsWith(base)) throw Error('Unhosted module import: ' + url);
    const module = new vm.SourceTextModule(fs.readFileSync(file(url), 'utf8'), {context, identifier: url});
    cache.set(url, module);
    await module.link((spec, ref) => load(new URL(spec, ref.identifier).href));
    return module;
  }
  const source = fs.readFileSync(file(url), 'utf8');
  const originalStart = source.search(/import\s*\{\s*cheerio,\s*模板\s*\}\s*from/);
  if (source.includes('Local compatibility for the legacy drpy parser API') && originalStart > 0) {
    const original = new vm.SourceTextModule(source.slice(originalStart), {context, identifier: url + '#original'});
    await original.link((spec, ref) => load(new URL(spec, ref.identifier).href));
    let reproduced = false;
    try { await original.evaluate(); } catch (error) { reproduced = /pdfh is not defined/.test(error.message); }
    if (!reproduced) throw Error('Expected original missing pdfh error');
    const compatibility = new vm.SourceTextModule(source.slice(0, originalStart) + '\nexport {pdfh,pdfa,pd};', {context, identifier: url + '#compat'});
    await compatibility.link((spec, ref) => load(new URL(spec, ref.identifier).href));
    await compatibility.evaluate();
    const html = '<div class="card"><a href="/movie/1">电影一</a><a href="/movie/2">电影二</a></div>';
    if (compatibility.namespace.pdfh(html, '.card&&a&&Text') !== '电影一') throw Error('Text parsing failed');
    if (compatibility.namespace.pdfa(html, '.card&&a').length !== 2) throw Error('List parsing failed');
    if (compatibility.namespace.pd(html, '.card&&a&&href', 'https://example.test/') !== 'https://example.test/movie/1') throw Error('URL resolution failed');
  }
  context = createContext();
  cache.clear();
  const engine = await load(url);
  await engine.evaluate();
  for (const name of ['init', 'home', 'homeVod', 'search']) {
    if (typeof engine.namespace.default[name] !== 'function') throw Error('Missing engine API: ' + name);
  }
  console.log('Legacy module/parser checks passed; this does not certify the FongMi player crawler contract.');
})().catch(error => { console.error(error); process.exit(1); });
