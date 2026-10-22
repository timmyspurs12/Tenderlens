import { Resvg } from '@resvg/resvg-js';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';

const INK='#111827', TEAL='#0F766E', IV='#F7F6F2', PLAT='#C5CDD5';
const OUT='dist'; mkdirSync(OUT,{recursive:true});

/* ── wordmark from exact font outlines ─────────────────────────────── */
const W = JSON.parse(readFileSync('fonts/wordmark.json','utf8'));
const UPM = W.unitsPerEm, CAPU = W.capHeight;
const TRACK = -18, TGAP = 16;
function wm(size, color, {x=0,y=0}={}) {
  const k = size/UPM; let ux=0; const parts=[];
  for (const ch of 'TenderLens') {
    const g = W.glyphs[ch];
    parts.push(`<path transform="translate(${(x+ux*k).toFixed(3)},${y}) scale(${k.toFixed(5)},${(-k).toFixed(5)})" d="${g.d}"/>`);
    ux += g.advance + TRACK;
    if (ch === 'r') ux += TGAP;
  }
  return { svg:`<g fill="${color}">${parts.join('')}</g>`, width:(ux+18)*k };
}

/* ── the mark ──────────────────────────────────────────────────────── */
const FIELD='M12 12H84V84H12Z';
const RULES=[[21,26,30],[21,38,20],[21,50,13],[21,62,9]], RH=5;
const AP={cx:64,cy:40.5,r:17};
const FOCUS={x:52,y:36.5,w:23,h:8};
const ruleRects = (f) => RULES.map(([x,y,w])=>`<rect x="${x}" y="${y}" width="${w}" height="${RH}" fill="${f}"/>`).join('');

function mark({field=TEAL, paper=IV, cut=false, id='m'}={}) {
  if (cut) return `<defs><mask id="${id}" maskUnits="userSpaceOnUse" x="0" y="0" width="96" height="96">
      <rect width="96" height="96" fill="#fff"/>${ruleRects('#000')}<circle cx="${AP.cx}" cy="${AP.cy}" r="${AP.r}" fill="#000"/>
    </mask></defs>
    <path d="${FIELD}" fill="${field}" mask="url(#${id})"/>
    <rect x="${FOCUS.x}" y="${FOCUS.y}" width="${FOCUS.w}" height="${FOCUS.h}" fill="${field}"/>`;
  return `<path d="${FIELD}" fill="${field}"/>${ruleRects(paper)}
    <circle cx="${AP.cx}" cy="${AP.cy}" r="${AP.r}" fill="${paper}"/>
    <rect x="${FOCUS.x}" y="${FOCUS.y}" width="${FOCUS.w}" height="${FOCUS.h}" fill="${field}"/>`;
}
const place=(body,size,x,y)=>`<g transform="translate(${x},${y}) scale(${size/96})">${body}</g>`;
const doc=(w,h,body,bg=null)=>`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${bg?`<rect width="${w}" height="${h}" fill="${bg}"/>`:''}${body}</svg>`;
const png=(svg,width,file,fonts=[])=>writeFileSync(file,new Resvg(svg,{fitTo:{mode:'width',value:width},font:fonts.length?{fontFiles:fonts,loadSystemFonts:false}:undefined}).render().asPng());

/* ── lockups ───────────────────────────────────────────────────────── */
const SYM=72, GAP=25, CAP=45, FS=(CAP/CAPU)*UPM;

function lockup({dark=false, mono=false}={}) {
  const field = mono ? (dark?IV:INK) : (dark?TEAL:TEAL);
  const paper = mono ? 'none' : (dark?INK:IV);
  const body = mono ? mark({field,cut:true}) : mark({field,paper:field===TEAL?paper:IV});
  const ink = mono ? (dark?IV:INK) : (dark?IV:INK);
  const w = wm(FS, ink, {x:SYM+GAP, y:(96-SYM)/2 + SYM/2 + CAP/2});
  return { body:`${place(body,SYM,0,(96-SYM)/2)}${w.svg}`, w:Math.ceil(SYM+GAP+w.width), h:96 };
}

/* ══ assets ══ */
const files = {};

// primary lockup
{
  const l = lockup({});
  files['tenderlens-logo-primary.svg'] = doc(l.w,l.h,l.body);
  png(doc(l.w,l.h,l.body,IV), 2400, `${OUT}/tenderlens-logo-primary.png`);
  png(doc(l.w,l.h,l.body), 2400, `${OUT}/tenderlens-logo-primary-transparent.png`);
}
// reversed
{
  const l = lockup({dark:true});
  files['tenderlens-logo-reversed.svg'] = doc(l.w,l.h,l.body);
  png(doc(l.w,l.h,l.body,INK), 2400, `${OUT}/tenderlens-logo-reversed.png`);
  png(doc(l.w,l.h,l.body), 2400, `${OUT}/tenderlens-logo-reversed-transparent.png`);
}
// monochrome
{
  const l = lockup({mono:true});
  files['tenderlens-logo-monochrome.svg'] = doc(l.w,l.h,l.body);
  png(doc(l.w,l.h,l.body,IV), 2400, `${OUT}/tenderlens-logo-monochrome.png`);
}
// symbol
files['tenderlens-symbol.svg'] = doc(96,96,mark({}));
png(doc(96,96,mark({})), 1024, `${OUT}/tenderlens-symbol.png`);

// app icons (rounded square, safe padding)
{
  const icon = doc(512,512,`<rect width="512" height="512" rx="112" fill="${TEAL}"/>${place(mark({field:TEAL}),336,88,88)}`);
  files['tenderlens-app-icon.svg'] = icon;
  png(icon, 1024, `${OUT}/tenderlens-app-icon.png`);
  const iconD = doc(512,512,`<rect width="512" height="512" rx="112" fill="${INK}"/>${place(mark({field:IV,paper:INK}),336,88,88)}`);
  png(iconD, 1024, `${OUT}/tenderlens-app-icon-reversed.png`);
}
// favicons — 16 drawn separately
{
  const fav16 = doc(32,32,`<path d="M1.5 1.5H30.5V30.5H1.5Z" fill="${TEAL}"/>
    <rect x="5.5" y="7.7" width="12" height="3.6" fill="${IV}"/>
    <rect x="5.5" y="14.8" width="7.5" height="3.6" fill="${IV}"/>
    <circle cx="21.3" cy="12.6" r="7.6" fill="${IV}"/>
    <rect x="15.4" y="10.5" width="11" height="4.2" fill="${TEAL}"/>`);
  files['tenderlens-favicon-16.svg'] = fav16;
  png(fav16, 16, `${OUT}/tenderlens-favicon-16.png`);
  const fav = doc(96,96,mark({}));
  files['tenderlens-favicon.svg'] = fav;
  for (const px of [32,48,64]) png(fav, px, `${OUT}/tenderlens-favicon-${px}.png`);
  png(fav, 512, `${OUT}/tenderlens-favicon-512.png`);
}

// 1024² portal upload — symbol dominant, wordmark beneath, 2 MB ceiling met
{
  const sym = 420;
  const wsize = 150;
  const wordmark = wm(wsize, INK);
  const gap = 92;
  const blockH = sym + gap + wsize*CAPU/UPM;
  const top = (1024 - blockH)/2;
  const body = `${place(mark({}), sym, (1024-sym)/2, top)}${wm(wsize, INK, {x:(1024-wordmark.width)/2, y:top+sym+gap+wsize*CAPU/UPM}).svg}`;
  const svg = doc(1024,1024,body,IV);
  files['tenderlens-portal-logo-1024.svg'] = svg;
  png(svg, 1024, `${OUT}/tenderlens-portal-logo-1024.png`);
}

// write SVGs
for (const [name, svg] of Object.entries(files)) writeFileSync(`${OUT}/${name}`, svg);

/* ══ identity sheet ══ */
{
  const SW=1600, M=130, INNER=SW-M*2;
  const F=['./node_modules/@expo-google-fonts/instrument-sans/500Medium/InstrumentSans_500Medium.ttf'];
  const L=(t,c='#6B7280')=>`<text x="${M}" y="0" font-family="Instrument Sans" font-size="13" font-weight="500" letter-spacing="2.4" fill="${c}">${t}</text>`;
  const blocks=[]; let y=170;
  const label=(t,c)=>{blocks.push(`<g transform="translate(0,${y})">${L(t,c)}</g>`); y+=36;};
  const rule=()=>{blocks.push(`<rect x="${M}" y="${y}" width="${INNER}" height="1" fill="${PLAT}" opacity=".6"/>`); y+=78;};

  label('01   PRIMARY LOCKUP · MIDNIGHT INK ON WARM IVORY');
  const p1=lockup({}); { const sc=1060/p1.w; blocks.push(`<g transform="translate(${M},${y}) scale(${sc})">${p1.body}</g>`); y+=p1.h*sc+70; }
  rule();

  label('02   REVERSED · ON MIDNIGHT INK');
  const d1=lockup({dark:true}); { const sc=640/d1.w, ph=330;
    blocks.push(`<rect x="${M}" y="${y}" width="${INNER}" height="${ph}" fill="${INK}"/>`);
    blocks.push(`<g transform="translate(${M+96},${y+(ph-d1.h*sc)/2}) scale(${sc})">${d1.body}</g>`); y+=ph+70; }
  rule();

  label('03   MONOCHROME · ONE COLOUR, TRUE HOLES');
  const m1=lockup({mono:true}); { const sc=560/m1.w; blocks.push(`<g transform="translate(${M},${y}) scale(${sc})">${m1.body}</g>`); y+=m1.h*sc+70; }
  rule();

  label('04   APP ICON · SYMBOL · 32 / 16 PX');
  {
    const icon=`<rect width="512" height="512" rx="112" fill="${TEAL}"/>${place(mark({field:TEAL}),336,88,88)}`;
    const iconD=`<rect width="512" height="512" rx="112" fill="${INK}"/>${place(mark({field:IV,paper:INK}),336,88,88)}`;
    const fav16svg = files['tenderlens-favicon-16.svg'].replace(/^<svg[^>]*>|<\/svg>$/g,'');
    blocks.push(`<g transform="translate(${M},${y})">
      <svg width="140" height="140" viewBox="0 0 512 512">${icon}</svg>
      <svg x="170" width="140" height="140" viewBox="0 0 512 512">${iconD}</svg>
      <svg x="352" y="22" width="96" height="96" viewBox="0 0 96 96">${mark({})}</svg>
      <svg x="482" y="54" width="32" height="32" viewBox="0 0 96 96">${mark({})}</svg>
      <svg x="548" y="70" width="16" height="16" viewBox="0 0 32 32">${fav16svg}</svg>
    </g>`); y+=140+66;
  }
  rule();

  label('05   PALETTE');
  { const sw=[['MIDNIGHT INK',INK,'#111827'],['DEEP PETROL TEAL',TEAL,'#0F766E'],['WARM IVORY',IV,'#F7F6F2'],['MUTED PLATINUM',PLAT,'#C5CDD5']];
    sw.forEach(([n,h,c],i)=>{const x=M+i*240;
      blocks.push(`<rect x="${x}" y="${y}" width="200" height="100" fill="${h}" stroke="${PLAT}" stroke-opacity=".7"/>
      <text x="${x}" y="${y+134}" font-family="Instrument Sans" font-size="12" font-weight="600" letter-spacing="1.7" fill="${INK}">${n}</text>
      <text x="${x}" y="${y+156}" font-family="Instrument Sans" font-size="12" fill="#6B7280">${c}</text>`);});
    y+=100+206; }
  rule();

  label('06   RULES OF CONSTRUCTION');
  [ 'FIELD · 96-unit grid, 12–84 solid. Four clause rules, 5 units, on a 12-unit rhythm.',
    'APERTURE · r17 at (64, 40.5), tangent to the field edge, centred on the second rule.',
    'FOCUS · the same rule again inside the glass, 23 × 8, one axis, two weights.',
    'WORDMARK · Instrument Sans SemiBold, −0.018em, wide air before the capital L.',
    'MINIMUMS · lockup 96 px wide; symbol 32 px; the 16 px favicon is drawn separately.',
  ].forEach((t,i)=>blocks.push(`<text x="${M}" y="${y+i*30}" font-family="Instrument Sans" font-size="13" fill="#4B5563">${t}</text>`));
  y+=5*30+10;

  const SH=y+50;
  const sheet=doc(SW,SH,`<rect width="${SW}" height="${SH}" fill="${IV}"/>
    <g transform="translate(0,86)">
      <text x="${M}" y="0" font-family="Instrument Sans" font-size="12" font-weight="600" letter-spacing="3.4" fill="${INK}">TENDERLENS</text>
      <text x="${M+142}" y="0" font-family="Instrument Sans" font-size="12" letter-spacing="3.4" fill="#9CA3AF">BRAND IDENTITY</text>
      <text x="${SW-M}" y="0" text-anchor="end" font-family="Instrument Sans" font-size="12" letter-spacing="2.4" fill="${PLAT}">SYMBOL · WORDMARK · RULES OF USE</text>
      <rect x="${M}" y="22" width="${INNER}" height="1" fill="${PLAT}" opacity=".6"/>
    </g>${blocks.join('\n')}`);
  files['tenderlens-identity-sheet.svg']=sheet;
  png(sheet, SW, `${OUT}/tenderlens-identity-sheet.png`, F);
  writeFileSync(`${OUT}/tenderlens-identity-sheet.svg`, sheet);
}

console.log('assets:', Object.keys(files).length + 6);
