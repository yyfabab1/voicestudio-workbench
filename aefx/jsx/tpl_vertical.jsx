/* ============================================================================
 * TEMPLATE 4 / vertical - portrait short-form (1080x1920)
 *
 * The phone layout: cover dominates the top half and breathes slowly, the
 * spectrum sits under it as the "music is playing" tell, and two lines of lyric
 * live in the lower third where a thumb does not cover them.
 * Works at any size, but it is drawn for tall frames.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_vertical", { base: CTX.bg1 || [0.04, 0.04, 0.08] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;
    var portrait = H > W;
    var box = rtMin(W, H * (portrait ? 0.46 : 0.62)) * 0.86;

    rtGlowBlob(comp, CTX.accent || [0.35, 0.45, 0.95], W * 0.5, H * 0.30, box * 0.75, 26);
    rtGlowBlob(comp, CTX.bg2 || [0.6, 0.25, 0.7], W * 0.85, H * 0.55, box * 0.6, 18);

    /* cover card, slowly zooming so a still image is not a dead rectangle */
    var cardY = H * 0.28;
    var pic = null;
    if (CTX.cover) pic = rtImage(comp, CTX.cover, box, box, {});
    if (pic) {
        rtPos(pic).setValue([W / 2, cardY]);
        var pz = rtScale(pic);
        pz.setValueAtTime(0, [100, 100]);
        pz.setValueAtTime(DUR, [112, 112]);
        rtOpa(pic).setValue(92);
    } else {
        var fb = rtSolid(comp, [CTX.accent ? CTX.accent[0] * 0.85 : 0.3,
                                CTX.accent ? CTX.accent[1] * 0.85 : 0.3,
                                CTX.accent ? CTX.accent[2] : 0.9],
                         "cardFallback", DUR);
        rtCircleMask(fb, W / 2, cardY, box * 0.48);
        rtOpa(fb).setValue(85);
    }

    /* spectrum: the tell that something is actually playing */
    if (AL) {
        var sp = rtSpectrum(comp, AL, {
            name: "bars", bands: 26, maxH: Math.round(H * 0.34 * (CTX.hScale || 1)), thick: 9,
            soft: 0.5, fStart: 45, fEnd: 760, display: 1, side: 1,
            inner: [CTX.accent ? CTX.accent[0] : 0.5, CTX.accent ? CTX.accent[1] : 0.8,
                    CTX.accent ? CTX.accent[2] : 1, 1],
            outer: [0.15, 0.2, 0.6, 1], dynHue: 0,
            p0: [0, H * 0.66], p1: [W, H * 0.66]
        });
        rtOpa(sp.layer).setValue(85);
        if (sp.errs.length) RT.specErr = sp.errs.join(",");
    }

    /* two lyric lines, swapped on the beat */
    var fs = Math.round(H * 0.045);
    var subFS = Math.round(H * 0.026);
    var lines = CTX.lines || [];
    var lycY = H * 0.78;
    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];
        var sub = ln[3] ? ln[2] : "";
        var L = rtText(comp, main, {
            name: "ln" + i, size: fs, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", pos: [W / 2, sub ? lycY - H * 0.02 : lycY], bold: true
        });
        L.startTime = 0; L.inPoint = a; L.outPoint = b;
        rtFadeIn(L, a, 0.14, 100);
        rtOpa(L).setValueAtTime(b - 0.14, 100);
        rtOpa(L).setValueAtTime(b, 0);
        if (sub) {
            var SL = rtText(comp, sub, {
                name: "sub" + i, size: subFS, font: CTX.font,
                color: [CTX.accent ? CTX.accent[0] : 0.8, CTX.accent ? CTX.accent[1] : 0.8,
                        CTX.accent ? CTX.accent[2] : 0.8, 1],
                justify: "c", pos: [W / 2, lycY + H * 0.032]
            });
            SL.startTime = 0; SL.inPoint = a; SL.outPoint = b;
            rtFadeIn(SL, a + 0.04, 0.18, 72);
            rtOpa(SL).setValueAtTime(b - 0.14, 72);
            rtOpa(SL).setValueAtTime(b, 0);
        }
    }

    if (CTX.title) {
        var hd = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.032), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.075], bold: true
        });
        hd.startTime = 0; rtOpa(hd).setValue(90);
    }
    if (CTX.subtitle) {
        var stl = rtText(comp, CTX.subtitle, {
            name: "subtitle", size: Math.round(H * 0.021), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.108]
        });
        stl.startTime = 0; rtOpa(stl).setValue(50);
    }
    return comp;
}
