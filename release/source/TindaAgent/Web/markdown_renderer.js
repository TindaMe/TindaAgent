/*
 * markdown_renderer.js
 *
 * 用处：把原本嵌在 chat.html 的 markdown 渲染函数抽出来，供 home.html 与 chat.html 共享。
 * 暴露：window.MarkdownRenderer + 一组向后兼容的全局函数（renderMarkdown / escapeHtml / ...）。
 *
 * 设计：纯函数、无外部依赖、无 DOM 副作用。返回值均为字符串 HTML。
 */
(function () {
  "use strict";

  function escapeHtml(text) {
    return String(text ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll("\"", "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeHref(rawUrl) {
    const url = String(rawUrl ?? "").trim();
    if (/^(https?:\/\/|mailto:|toolskip:)/i.test(url)) return url;
    return "#";
  }

  function renderInlineMarkdown(line) {
    const codeSpans = [];
    let s = String(line ?? "").replace(/`([^`]+)`/g, (_, code) => {
      const token = `@@INLINE_CODE_${codeSpans.length}@@`;
      codeSpans.push(`<code>${escapeHtml(code)}</code>`);
      return token;
    });

    s = escapeHtml(s);
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
      const href = safeHref(url);
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");

    codeSpans.forEach((html, i) => {
      s = s.replaceAll(`@@INLINE_CODE_${i}@@`, html);
    });
    return s;
  }

  function parseTableCells(line) {
    let s = String(line ?? "").trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    const token = "@@ESCAPED_PIPE@@";
    s = s.replace(/\\\|/g, token);
    return s.split("|").map((cell) => cell.replaceAll(token, "|").trim());
  }

  function isTableSeparatorLine(line) {
    const s = String(line ?? "").trim();
    if (!s || !s.includes("|")) return false;
    const cells = parseTableCells(s);
    if (!cells.length) return false;
    // 兼容常见 markdown 变体：允许 :--: / --: / :--
    // （标准多为 3 个以上横杠，但不少模型会产出 2 个横杠）
    return cells.every((cell) => /^:?-{2,}:?$/.test(cell));
  }

  function findNextNonEmptyLine(lines, startIndex, maxBlankLines) {
    const blankLimit = Number.isFinite(Number(maxBlankLines)) ? Number(maxBlankLines) : 0;
    let blankCount = 0;
    for (let i = startIndex; i < lines.length; i++) {
      const trim = String(lines[i] ?? "").trim();
      if (trim) return { index: i, trim };
      blankCount += 1;
      if (blankCount > blankLimit) break;
    }
    return null;
  }

  function isLoosePipeTableLine(line, expectedCellCount) {
    const s = String(line ?? "").trim();
    if (!s || !s.includes("|") || /^@@CODE_BLOCK_\d+@@$/.test(s)) return false;
    if (isTableSeparatorLine(s)) return false;
    const cells = parseTableCells(s);
    if (cells.length < 2) return false;
    if (expectedCellCount && cells.length !== expectedCellCount) return false;
    return cells.every((cell) => cell.length > 0);
  }

  function findLooseTableStart(lines, headerIndex) {
    const header = String(lines[headerIndex] ?? "").trim();
    if (!isLoosePipeTableLine(header, 0)) return null;
    const headerCells = parseTableCells(header);
    const next = findNextNonEmptyLine(lines, headerIndex + 1, 1);
    if (!next || isTableSeparatorLine(next.trim)) return null;
    if (!isLoosePipeTableLine(next.trim, headerCells.length)) return null;
    return next.index;
  }

  function parseTableAlign(cell) {
    const s = String(cell ?? "").trim();
    if (s.startsWith(":") && s.endsWith(":")) return "center";
    if (s.endsWith(":")) return "right";
    return "left";
  }

  function renderTableHtml(headers, aligns, rows) {
    const thHtml = headers.map((h, idx) => `<th style="text-align:${aligns[idx] || "left"}">${renderInlineMarkdown(h)}</th>`).join("");
    const trHtml = rows.map((cols) => {
      const tds = headers.map((_, idx) => {
        const cellText = cols[idx] ?? "";
        return `<td style="text-align:${aligns[idx] || "left"}">${renderInlineMarkdown(cellText)}</td>`;
      }).join("");
      return `<tr>${tds}</tr>`;
    }).join("");
    const tbody = trHtml ? `<tbody>${trHtml}</tbody>` : "";
    return `<div class="md-table-wrap"><table><thead><tr>${thHtml}</tr></thead>${tbody}</table></div>`;
  }

  function stashCodeBlock(codeBlocks, code, lang) {
    const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : "";
    const token = `@@CODE_BLOCK_${codeBlocks.length}@@`;
    const html = `<pre><code${langClass}>${escapeHtml(code)}</code></pre>`;
    codeBlocks.push(html);
    return token;
  }

  function renderMarkdown(text) {
    const source = String(text ?? "").replace(/\r\n/g, "\n");
    const codeBlocks = [];

    let body = source.replace(/```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g, (_, lang, code) => {
      return stashCodeBlock(codeBlocks, code, lang);
    });

    body = body.replace(/\[code(?:=([a-zA-Z0-9_-]+))?\]\n?([\s\S]*?)\n?\[\/code\]/gi, (_, lang, code) => {
      return stashCodeBlock(codeBlocks, code, lang);
    });

    const lines = body.split("\n");
    const parts = [];
    let inList = false;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trimEnd();
      const trim = line.trim();

      if (!trim) {
        if (inList) { parts.push("</ul>"); inList = false; }
        continue;
      }

      const nextTrim = i + 1 < lines.length ? lines[i + 1].trim() : "";
      if (trim.includes("|") && nextTrim && isTableSeparatorLine(nextTrim)) {
        if (inList) { parts.push("</ul>"); inList = false; }

        const headers = parseTableCells(trim);
        const alignDefs = parseTableCells(nextTrim);
        const aligns = headers.map((_, idx) => parseTableAlign(alignDefs[idx] || ""));
        const rows = [];

        i += 2;
        for (; i < lines.length; i++) {
          const rowTrim = lines[i].trim();
          if (!rowTrim) {
            i -= 1;
            break;
          }
          if (!rowTrim.includes("|") || /^@@CODE_BLOCK_\d+@@$/.test(rowTrim)) {
            i -= 1;
            break;
          }
          rows.push(parseTableCells(rowTrim));
        }

        parts.push(renderTableHtml(headers, aligns, rows));
        continue;
      }

      const looseTableRowStart = findLooseTableStart(lines, i);
      if (looseTableRowStart !== null) {
        if (inList) { parts.push("</ul>"); inList = false; }

        const headers = parseTableCells(trim);
        const aligns = headers.map(() => "left");
        const rows = [];
        let j = looseTableRowStart;

        while (j < lines.length) {
          const rowTrim = lines[j].trim();
          if (!rowTrim) {
            const next = findNextNonEmptyLine(lines, j + 1, 1);
            if (next && isLoosePipeTableLine(next.trim, headers.length)) {
              j = next.index;
              continue;
            }
            break;
          }
          if (!isLoosePipeTableLine(rowTrim, headers.length)) break;
          rows.push(parseTableCells(rowTrim));
          j += 1;
        }

        i = j - 1;
        parts.push(renderTableHtml(headers, aligns, rows));
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trim)) {
        if (inList) { parts.push("</ul>"); inList = false; }
        parts.push("<hr/>");
        continue;
      }

      const listMatch = /^[-*+]\s+(.+)$/.exec(trim);
      if (listMatch) {
        if (!inList) { parts.push("<ul>"); inList = true; }
        parts.push(`<li>${renderInlineMarkdown(listMatch[1])}</li>`);
        continue;
      }

      if (inList) { parts.push("</ul>"); inList = false; }

      const headingMatch = /^(#{1,6})\s+(.+)$/.exec(trim);
      if (headingMatch) {
        const level = headingMatch[1].length;
        parts.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
        continue;
      }

      if (trim.startsWith(">")) {
        if (inList) { parts.push("</ul>"); inList = false; }
        const quoteLines = [];
        for (; i < lines.length; i++) {
          const qTrim = lines[i].trim();
          if (!qTrim.startsWith(">")) {
            i -= 1;
            break;
          }
          const m = /^>\s?(.*)$/.exec(qTrim);
          quoteLines.push(m ? m[1] : "");
        }
        const quoteHtml = quoteLines
          .map((line) => (line ? renderInlineMarkdown(line) : "<br/>"))
          .join("<br/>");
        parts.push(`<blockquote>${quoteHtml}</blockquote>`);
        continue;
      }

      if (/^@@CODE_BLOCK_\d+@@$/.test(trim)) { parts.push(trim); continue; }

      parts.push(`<p>${renderInlineMarkdown(trim)}</p>`);
    }

    if (inList) parts.push("</ul>");

    let html = parts.join("");
    codeBlocks.forEach((block, i) => { html = html.replaceAll(`@@CODE_BLOCK_${i}@@`, block); });
    return html || "<p></p>";
  }

  // 公开命名空间
  window.MarkdownRenderer = {
    render: renderMarkdown,
    renderInline: renderInlineMarkdown,
    escapeHtml: escapeHtml,
    safeHref: safeHref,
    parseTableCells: parseTableCells,
    isTableSeparatorLine: isTableSeparatorLine,
    findNextNonEmptyLine: findNextNonEmptyLine,
    isLoosePipeTableLine: isLoosePipeTableLine,
    findLooseTableStart: findLooseTableStart,
    parseTableAlign: parseTableAlign,
  };

  // 向后兼容：chat.html 旧调用点（renderMarkdown / escapeHtml / safeHref / renderInlineMarkdown）继续可用
  if (typeof window.renderMarkdown !== "function") window.renderMarkdown = renderMarkdown;
  if (typeof window.escapeHtml !== "function") window.escapeHtml = escapeHtml;
  if (typeof window.safeHref !== "function") window.safeHref = safeHref;
  if (typeof window.renderInlineMarkdown !== "function") window.renderInlineMarkdown = renderInlineMarkdown;
  if (typeof window.parseTableCells !== "function") window.parseTableCells = parseTableCells;
  if (typeof window.isTableSeparatorLine !== "function") window.isTableSeparatorLine = isTableSeparatorLine;
  if (typeof window.findNextNonEmptyLine !== "function") window.findNextNonEmptyLine = findNextNonEmptyLine;
  if (typeof window.isLoosePipeTableLine !== "function") window.isLoosePipeTableLine = isLoosePipeTableLine;
  if (typeof window.findLooseTableStart !== "function") window.findLooseTableStart = findLooseTableStart;
  if (typeof window.parseTableAlign !== "function") window.parseTableAlign = parseTableAlign;
})();
