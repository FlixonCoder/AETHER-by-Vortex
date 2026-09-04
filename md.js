/* ===========================================================================
   AETHER — minimal Markdown renderer for the runbook viewer
   ---------------------------------------------------------------------------
   There is no build step in this project, so rather than vendor a 40 KB
   Markdown library this covers exactly what `agents/runbook_generator.py`
   emits: ATX headings, bold/italic, inline code, fenced code, ordered and
   unordered lists, GFM tables, blockquotes, horizontal rules and links.

   Security: every input character is HTML-escaped FIRST, then a fixed set of
   markdown constructs is re-introduced as tags. Raw HTML in the source is
   therefore rendered as visible text, never executed. Runbook content is
   partly LLM-generated, so this ordering is not optional.

   Usage:  element.innerHTML = AetherMD.render(markdownString);
   =========================================================================== */

(function (global) {
  'use strict';

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Inline: run on already-escaped text. Code spans are extracted first so
  // their contents are not further transformed.
  function inline(text) {
    const spans = [];
    let t = text.replace(/`([^`]+)`/g, (_, code) => {
      spans.push('<code>' + code + '</code>');
      return '\u0000' + (spans.length - 1) + '\u0000';
    });

    t = t
      .replace(/\[([^\]]+)\]\((https?:[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_]+)__/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      // [CMD] / [MON] / [VER] markers the runbook generator emits.
      .replace(/\[(CMD|MON|VER)\]/g, '<span class="md-tag md-tag-$1">$1</span>');

    return t.replace(/\u0000(\d+)\u0000/g, (_, i) => spans[+i]);
  }

  function splitRow(line) {
    return line.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  }

  function render(src) {
    if (!src) return '<p class="md-empty">(empty)</p>';

    const lines = esc(src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code
      const fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
        i++;
        out.push('<pre class="md-pre"><code>' + body.join('\n') + '</code></pre>');
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }

      // Heading
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { out.push('<h' + h[1].length + ' class="md-h">' + inline(h[2]) + '</h' + h[1].length + '>'); i++; continue; }

      // Table: header row followed by a separator row
      if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) rows.push(splitRow(lines[i++]));
        out.push(
          '<table class="md-table"><thead><tr>' +
          head.map((c) => '<th>' + inline(c) + '</th>').join('') +
          '</tr></thead><tbody>' +
          rows.map((r) => '<tr>' + r.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') +
          '</tbody></table>'
        );
        continue;
      }

      // Blockquote
      // NOTE: esc() has already turned '>' into '&gt;' by this point.
      if (/^\s*&gt;\s?/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) body.push(lines[i++].replace(/^\s*&gt;\s?/, ''));
        out.push('<blockquote class="md-quote">' + inline(body.join(' ')) + '</blockquote>');
        continue;
      }

      // Lists
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          items.push(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
        }
        const tag = ordered ? 'ol' : 'ul';
        out.push('<' + tag + ' class="md-list">' +
          items.map((t) => '<li>' + inline(t) + '</li>').join('') +
          '</' + tag + '>');
        continue;
      }

      // Blank
      if (!line.trim()) { i++; continue; }

      // Paragraph
      const para = [];
      while (i < lines.length && lines[i].trim() &&
             !/^(\s*(#{1,6}\s|```|&gt;|[-*+]\s|\d+\.\s))/.test(lines[i])) {
        para.push(lines[i++]);
      }
      out.push('<p class="md-p">' + inline(para.join(' ')) + '</p>');
    }

    return out.join('\n');
  }

  global.AetherMD = { render: render };
})(window);
