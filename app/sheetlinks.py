#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 kdocs 表格里完整提取：剧名 / 集数 / 画质 / 百度链接 / 夸克链接。

原理：表格前端暴露了 WPSOpenApi.KSheetApplication，可以按 Range 读单元格
（`Range('F10').Text`）和超链接（`Range('F10').Hyperlinks.Item(1).Address`）。
"""
import time


def extract_sheet_rows(page, max_rows: int = 400, log=print) -> list[dict]:
    js = r"""
    async (maxRows) => {
      const race = (p, ms) => Promise.race([Promise.resolve(p),
        new Promise(r => setTimeout(() => r('TIMEOUT'), ms))]);
      const flat = (v) => {
        if (v === null || v === undefined) return '';
        if (Array.isArray(v)) return flat(v[0]);
        return String(v).trim();
      };
      const api = window.WPSOpenApi;
      if (!api) return {error: 'no WPSOpenApi'};
      await race(api.documentReadyPromise, 10000);
      let app = api.KSheetApplication;
      if (typeof app === 'function' && typeof app.then !== 'function') app = app();
      app = await race(app, 8000);
      if (!app || app === 'TIMEOUT') return {error: 'no app'};
      const wb = await race(app.ActiveWorkbook, 5000);
      if (!wb || wb === 'TIMEOUT') return {error: 'no workbook'};

      const sheets = await race(wb.Worksheets, 5000);
      let nSheets = 1;
      try { const c = await race(sheets.Count, 3000); if (typeof c === 'number') nSheets = c; } catch (e) {}

      const out = [];
      for (let si = 1; si <= Math.min(nSheets, 8); si++) {
        let sh;
        try { sh = await race(sheets.Item(si), 4000); } catch (e) { continue; }
        if (!sh || sh === 'TIMEOUT') continue;
        let sname = '';
        try { sname = String(await race(sh.Name, 2000)).slice(0, 30); } catch (e) {}
        try { await race(sh.Activate(), 4000); } catch (e) {}

        const rng = (a) => {
          try { return sh.Range(a); } catch (e) { return null; }
        };
        const colVal = async (col) => {
          const r = rng(col + '2:' + col + maxRows);
          if (!r) return [];
          try { return (await race(r.Value2, 8000)) || []; } catch (e) { return []; }
        };
        const titles = await colVal('B');
        const cats   = await colVal('C');
        const eps    = await colVal('D');
        const quals  = await colVal('H');
        if (!titles.length) continue;

        for (let i = 0; i < titles.length; i++) {
          const title = flat(titles[i]);
          if (!title || title.length < 2) continue;
          if (/剧集名称|名称|更新列表|免责声明/.test(title)) continue;
          const row = i + 2;
          const linkOf = async (col) => {
            const r = rng(col + row);
            if (!r) return '';
            try {
              const c = await race(r.Hyperlinks.Count, 1200);
              if (typeof c !== 'number' || c < 1) return '';
              const h = await race(r.Hyperlinks.Item(1), 1500);
              if (!h || h === 'TIMEOUT') return '';
              const a = await race(h.Address, 2000);
              return (a && a !== 'TIMEOUT') ? String(a) : '';
            } catch (e) { return ''; }
          };
          const baidu = await linkOf('F');
          const quark = await linkOf('E');
          out.push({
            sheet: sname, row: row, title: title,
            eps: flat(eps[i]), quality: flat(quals[i]), cat: flat(cats[i]),
            baidu: baidu, quark: quark
          });
        }
      }
      return {rows: out, sheets: nSheets};
    }
    """
    return page.evaluate(js, max_rows) or {}
