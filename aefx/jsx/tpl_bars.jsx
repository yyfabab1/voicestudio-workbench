/* ============================================================================
 * TEMPLATE 5 / bars - AUDIO TEMPLATE #1 : straight bar spectrum
 *
 * The "audio first" layout: the spectrum is the largest object in frame, driven
 * by AE's own Audio Spectrum effect (matchName ADBE AudSpect - note it is NOT
 * "ADBE Audio Spectrum"), so it reacts to the real track with zero analysis on
 * our side. A second thin waveform strip rides underneath for texture.
 * The cover card floats above it and breathes; lyrics sit bottom centre.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_bars", { base: CTX.bg1 || [0.02, 0.03, 0.06] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;
    var acc = CTX.accent || [0.4, 0.6, 1];

    rtGlowBlob(comp, acc, W * 0.5, H * 0.52, W * 0.45, 24);
    rtGlowBlob(comp, CTX.bg2 || [0.5, 0.2, 0.6], W * 0.15, H * 0.25, W * 0.35, 18);

    /* --- main spectrum, mirrored so it reads from the middle outwards --- */
    if (AL) {
        var sp = rtSpectrum(comp, AL, {
            name: "barsMain", bands: 30, maxH: Math.round(H * 0.72 * (CTX.hScale || 1)), thick: 10,
            soft: 0.5, fStart: 45, fEnd: 760, display: 1, side: 3,
            inner: [acc[0], acc[1], acc[2], 1], outer: [0.1, 0.2, 0.55, 1],
            dynHue: 1, ms: 70,
            p0: [0, H * 0.80], p1: [W, H * 0.80]
        });
        rtOpa(sp.layer).setValue(88);
        if (sp.errs.length) RT.specErr = sp.errs.join(",");

        /* thin waveform ribbon: texture, not information */
        var wv = rtWave(comp, AL, {
            name: "ribbon", samples: 320, maxH: Math.round(H * 0.10),
            thick: 2, soft: 55, ms: 110,
            inner: [1, 1, 1, 1], outer: [acc[0], acc[1], acc[2], 1],
            p0: [0, H * 0.90], p1: [W, H * 0.90]
        });

        rtOpa(wv.layer).setValue(45);
    }

    /* --- cover card above the bars --- */
    var box = rtMin(W, H) * 0.34;
    var pic = CTX.cover ? rtImage(comp, CTX.cover, box, box, {}) : null;
    if (pic) {
        rtPos(pic).setValue([W / 2, H * 0.30]);
        var pz = rtScale(pic);
        pz.setValueAtTime(0, [100, 100]);
        pz.setValueAtTime(DUR, [108, 108]);
        rtOpa(pic).setValue(95);
    } else {
        rtGlowBlob(comp, acc, W / 2, H * 0.30, box * 0.5, 40);
    }

    /* --- lyric, one line at a time, bottom centre --- */
    var fs = Math.round(H * 0.048);
    var lines = CTX.lines || [];
    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];
        var L = rtText(comp, main, {
            name: "ln" + i, size: fs, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", pos: [W / 2, H * 0.755]
        });
        L.startTime = 0; L.inPoint = a; L.outPoint = b;
        rtFadeIn(L, a, 0.15, 100);
        rtOpa(L).setValueAtTime(b - 0.15, 100);
        rtOpa(L).setValueAtTime(b, 0);
    }

    if (CTX.title) {
        var hd = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.040), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.085], bold: true
        });
        hd.startTime = 0; rtOpa(hd).setValue(92);
    }
    if (CTX.subtitle) {
        var stl = rtText(comp, CTX.subtitle, {
            name: "subtitle", size: Math.round(H * 0.024), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.118]
        });
        stl.startTime = 0; rtOpa(stl).setValue(48);
    }
    return comp;
}
