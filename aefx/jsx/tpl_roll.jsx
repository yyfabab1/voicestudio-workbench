/* ============================================================================
 * TEMPLATE 2 / roll - the scrolling lyric wall
 *
 * Every line lives at its own slot in a tall column; a parent null slides that
 * column so the line being sung is always parked in the middle of the frame.
 * Lines above and below stay visible but dimmed, and the top/bottom of the
 * frame fades out through feathered masks so lines float in and out instead of
 * popping. This is the "whole song visible at once" reading experience.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_roll", { base: CTX.bg1 || [0.03, 0.04, 0.07] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;
    var lines = CTX.lines || [];

    rtGlowBlob(comp, CTX.accent || [0.30, 0.50, 0.95], W * 0.5, H * 0.12, W * 0.55, 22);

    /* vertical centre where the active line parks */
    var cy = H * 0.52;
    var gap = Math.round(H * 0.088);
    var root = comp.layers.addNull();
    root.name = "lyricRoot";
    root.threeDLayer = false;
    rtPos(root).setValue([0, 0]);

    var fs = Math.round(H * 0.052);
    var subFS = Math.round(H * 0.030);
    var cx = W / 2;

    function rectMask(layer, y0, y1, feather) {
        try {
            var m = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
            var shp = new Shape();
            // deliberately overshoot sideways: the horizontal edges sit outside
            // the frame so only the y0/y1 edges show their feather
            shp.vertices = [[-W, y0], [W * 2, y0], [W * 2, y1], [-W, y1]];
            shp.inTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
            shp.outTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
            shp.closed = true;
            m.property("ADBE Mask Shape").setValue(shp);
            m.property("ADBE Mask Feather").setValue([0, feather]);
            m.property("ADBE Mask Mode").setValue(MaskMode.ADD);
        } catch (e0) { }
    }

    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];
        var sub = ln[3] ? ln[2] : "";
        var col = ln[4] || CTX.accent || [1, 1, 1];

        var y = i * gap;                       // its slot inside the column
        var layer = rtText(comp, main, {
            name: "ln" + i, size: fs, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", parent: root, pos: [cx, y]
        });
        layer.startTime = 0;
        // dimmed while neighbours hold the stage, full brightness while this
        // line is the one parked in the middle
        var op = rtOpa(layer);
        var prev = i > 0 ? rtClamp(lines[i - 1][0], 0, DUR) : 0;
        op.setValueAtTime(0, 22);
        op.setValueAtTime(Math.max(prev, a - 0.45), 22);
        op.setValueAtTime(a, 100);
        op.setValueAtTime(Math.max(a + 0.05, b - 0.1), 100);
        op.setValueAtTime(b, 22);
        op.setValueAtTime(DUR, 22);

        if (sub) {
            var sl = rtText(comp, sub, {
                name: "sub" + i, size: subFS, font: CTX.font,
                color: [col[0], col[1], col[2], 1], justify: "c",
                parent: root, pos: [cx, y + gap * 0.42]
            });
            sl.startTime = 0;
            var sop = rtOpa(sl);
            sop.setValueAtTime(0, 14);
            sop.setValueAtTime(Math.max(prev, a - 0.45), 14);
            sop.setValueAtTime(a, 70);
            sop.setValueAtTime(b, 14);
            sop.setValueAtTime(DUR, 14);
        }
    }

    /* slide the column so line i sits on cy while it is being sung */
    var rp = rtPos(root);
    rp.setValueAtTime(0, [W / 2 - cx, cy - 0]);
    for (var j = 0; j < lines.length; j++) {
        var t0 = rtClamp(lines[j][0], 0, DUR);
        rp.setValueAtTime(t0, [W / 2 - cx, cy - j * gap]);
    }
    if (lines.length) {
        rp.setValueAtTime(DUR, [W / 2 - cx, cy - (lines.length - 1) * gap]);
    }

    /* frame fade: lines drift into the top/bottom instead of being cut */
    var top = rtSolid(comp, CTX.bg1 || [0.03, 0.04, 0.07], "scrimTop", DUR);
    rectMask(top, -H, H * 0.22, H * 0.10);
    var bot = rtSolid(comp, CTX.bg1 || [0.03, 0.04, 0.07], "scrimBot", DUR);
    rectMask(bot, H * 0.78, H * 1.4, H * 0.10);

    /* permanent header */
    if (CTX.title) {
        var hd = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.036), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.075], bold: true
        });
        hd.startTime = 0;
        rtOpa(hd).setValue(88);
    }
    /* progress bar */
    var barH = Math.max(3, Math.round(H * 0.005));
    var bar = rtSolid(comp, CTX.accent || [1, 1, 1], "progress", DUR);
    rectMask(bar, H - barH, H + 2, 0);
    rtAnchor(bar).setValue([0, H / 2]);
    rtPos(bar).setValue([0, H / 2]);
    rtOpa(bar).setValue(65);
    var bs = rtScale(bar);
    bs.setValueAtTime(0, [0.1, 100]);
    bs.setValueAtTime(DUR, [100, 100]);

    return comp;
}
