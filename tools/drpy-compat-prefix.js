
// Local compatibility for the legacy drpy parser API removed in TV 5.6.3.
import {load as compatLoad} from 'https://raw.githubusercontent.com/hjdhnx/dr_py/250833c9e652dd2b3e1ec1e52271459ad215abac/libs/cheerio.min.js';
function compatQuery(html, parse, first) {
    const $ = compatLoad(String(html || ''));
    let node = $.root();
    for (let part of parse.split('&&')) {
        part = part.trim();
        if (!part || part === 'body') continue;
        node = node.find(part);
        if (first) node = node.first();
    }
    return {$, node};
}
function pdfh(html, parse) {
    const parts = String(parse || '').split('&&');
    const attr = parts.pop();
    const {node} = compatQuery(html, parts.join('&&'), true);
    if (attr === 'Text') return node.text().trim();
    if (attr === 'Html') return node.html() || '';
    return node.attr(attr) || '';
}
function pdfa(html, parse) {
    const {$, node} = compatQuery(html, String(parse || ''), false);
    return node.toArray().map(element => $.html(element));
}
function pd(html, parse, base) {
    const value = pdfh(html, parse);
    return value ? joinUrl(base || '', value) : '';
}
console.log('[douban-repair] parser compatibility loaded; req=' + typeof req + ', local=' + typeof local);
