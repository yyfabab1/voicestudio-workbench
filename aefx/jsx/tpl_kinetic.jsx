/* ============================================================================
 * TEMPLATE 1 / kinetic - "card-dot lyrics"
 *
 * Borrows the language of a music-app lyric page: each line steps forward as it
 * arrives, the current line is the biggest thing on screen, everything else has
 * already faded behind it. Original line on top, translation underneath.
 * Lines are back-to-back: the previous one fades out while the next one is
 * already fading in, so there is never a dead gap in the middle of a verse.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_kinetic", { base: CTX.bg1 || [0.04, 0.05, 0.09] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;

    /* floor: two soft blobs + a slow breathe, otherwise the frame looks dead */
    rtGlowBlob(comp, CTX.accent || [0.35, 0.55, 0.95], W * 0.24, H * 0.30, W * 0.42, 26);
    rtGlowBlob(comp, CTX.bg2 || [0.55, 0.25, 0.75], W * 0.80, H * 0.72, W * 0.40, 22);
    var breath = rtScale(S.base);
    breath.setValueAtTime(0, [100, 100]);
    breath.setValueAtTime(DUR, [106, 106]);

    /* permanent title / subtitle */
    if (CTX.title) {
        var t = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.045), font: CTX.font,
            color: [1, 1, 1, 1], justify: "l", pos: [H * 0.055, H * 0.075], bold: true
        });
        t.startTime = 0;
        rtOpa(t).setValue(92);
    }
    if (CTX.subtitle) {
        var stt2 = rtText(comp, CTX.subtitle, {
            name: "subtitle", size: Math.round(H * 0.026), font: CTX.font,
            color: [1, 1, 1, 1], justify: "l", pos: [H * 0.055, H * 0.125]
        });
        stt2.startTime = 0;
        rtOpa(stt2).setValue(46);
    }

    /* one layer per lyric line */
    var bigFS = Math.round(H * 0.072);
    var subFS = Math.round(H * 0.038);
    var cx = W / 2, cy = H * 0.50;
    var lines = CTX.lines || [];
    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];      // prefer the translation
        var sub = ln[3] ? ln[2] : "";          // show the original underneath
        var col = ln[4] || CTX.accent || [1, 1, 1];

        var layer = rtText(comp, main, {
            name: "ln" + i, size: bigFS, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", pos: [cx, sub ? cy - H * 0.028 : cy]
        });
        layer.startTime = 0;
        layer.inPoint = a;
        layer.outPoint = b;
        var sc = rtScale(layer);
        sc.setValueAtTime(a, [94, 94]);
        sc.setValueAtTime(a + 0.22, [100, 100]);
        sc.setValueAtTime(b - 0.12, [100.5, 100.5]);
        rtFadeIn(layer, a, 0.16, 100);
        rtOpa(layer).setValueAtTime(b - 0.18, 100);
        rtOpa(layer).setValueAtTime(b, 0);

        if (sub) {
            var subLayer = rtText(comp, sub, {
                name: "sub" + i, size: subFS, font: CTX.font,
                color: [col[0], col[1], col[2], 1], justify: "c",
                pos: [cx, cy + H * 0.058]
            });
            subLayer.startTime = 0;
            subLayer.inPoint = a;
            subLayer.outPoint = b;
            rtFadeIn(subLayer, a + 0.05, 0.2, 78);
            rtOpa(subLayer).setValueAtTime(b - 0.18, 78);
            rtOpa(subLayer).setValueAtTime(b, 0);
        }
    }

    /* progress bar along the bottom, so people know how much song is left */
    var barH = Math.max(3, Math.round(H * 0.006));
    var bar = rtSolid(comp, CTX.accent || [1, 1, 1], "progress", DUR);
    try {
        var m = bar.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
        var shp = new Shape();
        shp.vertices = [[0, H - barH], [W, H - barH], [W, H], [0, H]];
        shp.inTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
        shp.outTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
        shp.closed = true;
        m.property("ADBE Mask Shape").setValue(shp);
        m.property("ADBE Mask Mode").setValue(MaskMode.ADD);
    } catch (e0) { }
    rtOpa(bar).setValue(70);
    rtAnchor(bar).setValue([0, H / 2]);
    rtPos(bar).setValue([0, H / 2]);
    var bs = rtScale(bar);
    bs.setValueAtTime(0, [0.1, 100]);
    bs.setValueAtTime(DUR, [100, 100]);

    return comp;
}
