/* ============================================================================
 * TEMPLATE 6 / ring - AUDIO TEMPLATE #2 : vinyl turntable
 *
 * WHY IT IS NOT A POLAR RING: AE's polar spectrum mode needs a real path to
 * wrap around. Measured three times on this box - with a script-built mask
 * path AE only ever draws a single tiny arrow at the path's origin, never a
 * crown of spikes. Rather than fake it, the record is a real record:
 *   - a platter + vinyl disc that turns once every 8s
 *   - the cover art as the centre label (so the rotation is visible)
 *   - grooves, a spindle hole and a static sheen
 *   - a tonearm that drifts inward
 * The spectrum is real (ADBE AudSpect) and stands at the bottom as a base.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_ring", { base: CTX.bg1 || [0.03, 0.03, 0.06] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;
    var acc = CTX.accent || [0.45, 0.55, 1];
    var hs = CTX.hScale || 1;

    var cx = W / 2, cy = H * 0.38;
    var R = rtMin(W, H) * 0.25;

    /* mask helpers local to this template ---------------------------------- */
    function ringMask(layer, r0, r1) {
        /* one thin ring = big ellipse ADD + smaller ellipse SUBTRACT */
        try {
            var mp = layer.property("ADBE Mask Parade");
            var a = mp.addProperty("ADBE Mask Atom");
            rtCircleMaskAt(a, r1);
            a.property("ADBE Mask Mode").setValue(MaskMode.ADD);
            var b = mp.addProperty("ADBE Mask Atom");
            rtCircleMaskAt(b, r0);
            b.property("ADBE Mask Mode").setValue(MaskMode.SUBTRACT);
        } catch (eR) { }
    }
    function rtCircleMaskAt(m, r) {
        var shp = new Shape();
        var pts = [], tI = [], tO = [];
        for (var i = 0; i < 24; i++) {
            var ang = (Math.PI * 2 * i) / 24;
            pts.push([cx + Math.cos(ang) * r, cy + Math.sin(ang) * r]);
            tI.push([0, 0]); tO.push([0, 0]);
        }
        shp.vertices = pts; shp.inTangents = tI; shp.outTangents = tO;
        shp.closed = true;
        m.property("ADBE Mask Shape").setValue(shp);
        m.property("ADBE Mask Feather").setValue([Math.max(1, r * 0.02), Math.max(1, r * 0.02)]);
    }
    function rectMask(layer, x0, y0, x1, y1) {
        try {
            var m = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
            var shp = new Shape();
            shp.vertices = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
            shp.inTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
            shp.outTangents = [[0, 0], [0, 0], [0, 0], [0, 0]];
            shp.closed = true;
            m.property("ADBE Mask Shape").setValue(shp);
            m.property("ADBE Mask Mode").setValue(MaskMode.ADD);
            return true;
        } catch (eM) { return false; }
    }
    /* full-comp solids live in comp space, so anchor=position=centre keeps the
     * placement identical while moving the rotation pivot onto the disc. */
    function spinAround(layer, px, py, turns) {
        rtAnchor(layer).setValue([px, py]);
        rtPos(layer).setValue([px, py]);
        var p = rtRot(layer);
        if (!p) return;
        try {
            p.setValueAtTime(0, 0);
            p.setValueAtTime(DUR, 360 * turns);
        } catch (eS) { }
    }

    rtGlowBlob(comp, acc, cx, cy, R * 2.6, 20);
    rtGlowBlob(comp, CTX.bg2 || [0.55, 0.2, 0.65], W * 0.13, H * 0.70, R * 1.6, 14);

    /* --- audio furniture (added early => sits behind the record) --- */
    if (AL) {
        var sp = rtSpectrum(comp, AL, {
            name: "pedestal", bands: 30, maxH: Math.round(H * 0.18 * hs), thick: 10,
            soft: 0.5, fStart: 45, fEnd: 760, display: 1, side: 1,
            inner: [acc[0], acc[1], acc[2], 1], outer: [0.1, 0.16, 0.5, 1],
            dynHue: 1, ms: 70,
            p0: [0, H * 0.93], p1: [W, H * 0.93]
        });
        rtOpa(sp.layer).setValue(80);
        if (sp.errs.length) RT.specErr = sp.errs.join(",");

        var wv = rtWave(comp, AL, {
            name: "ribbon", samples: 340, maxH: Math.round(H * 0.09),
            thick: 2, soft: 55, ms: 110,
            inner: [1, 1, 1, 1], outer: [acc[0], acc[1], acc[2], 1],
            p0: [0, H * 0.68], p1: [W, H * 0.68]
        });
        rtOpa(wv.layer).setValue(42);
    }

    /* --- turntable --- */
    var platter = rtSolid(comp, [0.13, 0.14, 0.18], "platter", DUR);
    rtCircleMask(platter, cx, cy, R * 1.18);
    rtOpa(platter).setValue(70);

    var vinyl = rtSolid(comp, [0.03, 0.035, 0.05], "vinyl", DUR);
    rtCircleMask(vinyl, cx, cy, R);
    rtOpa(vinyl).setValue(100);

    var grooves = rtSolid(comp, [1, 1, 1], "grooves", DUR);
    ringMask(grooves, R * 0.90, R * 0.935);
    ringMask(grooves, R * 0.78, R * 0.815);
    ringMask(grooves, R * 0.66, R * 0.695);
    ringMask(grooves, R * 0.54, R * 0.575);
    rtOpa(grooves).setValue(9);

    /* the label is what makes the rotation readable */
    var labelLayer = null;
    if (CTX.cover) labelLayer = rtImage(comp, CTX.cover, R * 1.16, R * 1.16, {});
    if (labelLayer) {
        rtPos(labelLayer).setValue([cx, cy]);
        var dsz = rtSourceSize(labelLayer);
        rtCircleMask(labelLayer, dsz[0] / 2, dsz[1] / 2,
                     rtMin(dsz[0], dsz[1]) * 0.49);
        var lp = rtRot(labelLayer);
        if (lp) {
            try {
                lp.setValueAtTime(0, 0);
                lp.setValueAtTime(DUR, 360 * (DUR / 8.0));
            } catch (eL) { }
        }
        rtOpa(labelLayer).setValue(100);
    } else {
        var fb = rtSolid(comp, [acc[0] * 0.85, acc[1] * 0.85, acc[2] * 0.95],
                         "labelFallback", DUR);
        rtCircleMask(fb, cx, cy, R * 0.58);
        rtOpa(fb).setValue(90);
        spinAround(fb, cx, cy, DUR / 8.0);
    }

    spinAround(vinyl, cx, cy, DUR / 8.0);
    spinAround(grooves, cx, cy, DUR / 8.0);

    /* spindle + static sheen (the sheen must NOT turn, that is the tell) */
    var hole = rtSolid(comp, [0.015, 0.015, 0.02], "spindle", DUR);
    rtCircleMask(hole, cx, cy, Math.max(2, R * 0.075));
    rtOpa(hole).setValue(100);

    var sheen = rtSolid(comp, [1, 1, 1], "sheen", DUR);
    try {
        var sm = sheen.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
        var ss = new Shape();
        var sv = [], sI = [], sO = [];
        for (var k = 0; k < 16; k++) {
            var a2 = (Math.PI * 2 * k) / 16;
            sv.push([cx - R * 0.34 + Math.cos(a2) * R * 0.52,
                     cy - R * 0.40 + Math.sin(a2) * R * 0.26]);
            sI.push([0, 0]); sO.push([0, 0]);
        }
        ss.vertices = sv; ss.inTangents = sI; ss.outTangents = sO; ss.closed = true;
        sm.property("ADBE Mask Shape").setValue(ss);
        sm.property("ADBE Mask Feather").setValue([R * 0.30, R * 0.30]);
        sm.property("ADBE Mask Mode").setValue(MaskMode.ADD);
    } catch (eSh) { }
    rtOpa(sheen).setValue(10);

    /* --- tonearm: pivot at the top right, drifting inward --- */
    try {
        var px = cx + R * 1.30, py = cy - R * 1.02;
        var tipX = cx + R * 0.18, tipY = cy - R * 0.06;
        var dx = tipX - px, dy = tipY - py;
        var len = Math.sqrt(dx * dx + dy * dy);
        if (len > 1) {
            var ux = dx / len, uy = dy / len;
            var nx = -uy, ny = ux;
            var th = R * 0.045;
            var arm = rtSolid(comp, [0.62, 0.64, 0.70], "tonearm", DUR);
            rectMask(arm,
                     px + nx * th, py + ny * th,
                     tipX + nx * th, tipY + ny * th);
            rectMask(arm,
                     tipX - nx * th, tipY - ny * th,
                     px - nx * th, py - ny * th);
            rtAnchor(arm).setValue([px, py]);
            rtPos(arm).setValue([px, py]);
            var rp = rtRot(arm);
            if (rp) {
                rp.setValueAtTime(0, 0);
                rp.setValueAtTime(DUR, -3.5);
            }
            rtOpa(arm).setValue(88);

            var piv = rtSolid(comp, [0.42, 0.44, 0.5], "pivot", DUR);
            rtCircleMask(piv, px, py, Math.max(2, R * 0.11));
            rtOpa(piv).setValue(95);
        }
    } catch (eA) { }

    /* --- lyric under the record --- */
    var fs = Math.round(H * 0.046);
    var lines = CTX.lines || [];
    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];
        var L = rtText(comp, main, {
            name: "ln" + i, size: fs, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", pos: [W / 2, H * 0.745]
        });
        L.startTime = 0; L.inPoint = a; L.outPoint = b;
        rtFadeIn(L, a, 0.15, 100);
        rtOpa(L).setValueAtTime(b - 0.15, 100);
        rtOpa(L).setValueAtTime(b, 0);
    }

    if (CTX.title) {
        var hd = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.038), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.075], bold: true
        });
        hd.startTime = 0; rtOpa(hd).setValue(92);
    }
    if (CTX.subtitle) {
        var stl = rtText(comp, CTX.subtitle, {
            name: "subtitle", size: Math.round(H * 0.022), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.108]
        });
        stl.startTime = 0; rtOpa(stl).setValue(46);
    }
    return comp;
}
