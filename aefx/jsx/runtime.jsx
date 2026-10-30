/* ============================================================================
 * VoiceStudio -> After Effects : runtime helper library
 *
 * Every render concatenates:  runtime.jsx  +  tpl_<name>.jsx  +  payload  +  driver
 * The template only has to implement TPL_build(CTX) and return a CompItem.
 * Queueing / output module / cleanup is done by the driver.
 *
 * ---- Facts measured on this machine (AE 2026 / 26.0x67, zh_CN) ----
 *  1) ExtendScript here is ES3: Array.forEach / map / indexOf / trim /
 *     Object.keys / global JSON ALL MISSING. Never use them.
 *  2) TextDocument.font wants a POSTSCRIPT name. Display names with spaces fail
 *     ("Microsoft YaHei" throws); "MicrosoftYaHei" works.
 *  3) Output module templates are LOCALISED. "H.264" fails,
 *     "H.264 - Match Render Settings - 15 Mbps" is the Chinese name seen on this
 *     box -> we probe a candidate list instead of hard-coding one English name.
 *  4) Project.saveAs DOES NOT EXIST in 26.0. We can never "save as" another file,
 *     so the only safe strategy is: snapshot item ids -> build -> render ->
 *     delete everything that was not there before. NEVER newProject().
 *  5) Audio effects: matchName is "ADBE AudSpect" / "ADBE AudWave".
 *     "ADBE Audio Spectrum" DOES NOT EXIST.
 * ==========================================================================*/

var RT = {};

/* ---------------- tiny ES3 helpers ---------------- */
function rtMin(a, b) { return a < b ? a : b; }
function rtMax(a, b) { return a > b ? a : b; }
function rtClamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

/* AE colour slots are picky and the two families disagree:
 *   TextDocument.fillColor / Solid colour need EXACTLY 3 components
 *   (passing [r,g,b,1] throws "colour array does not have 3 values"),
 *   while effect colour params generally want 3 but some builds take 4.
 * Callers may hand us either shape; everything funnels through here. */
function c3(c) {
    if (!c) return [1, 1, 1];
    if (c.length >= 3) return [c[0], c[1], c[2]];
    return [c[0] || 0, c[1] || 0, c[2] || 0];
}
function rtSetColor(prop, c) {
    try { prop.setValue([c[0], c[1], c[2]]); return true; }
    catch (e0) { }
    try { prop.setValue([c[0], c[1], c[2], 1]); return true; }
    catch (e1) { }
    return false;
}

function rtJustify(kind) {
    if (kind === "c") return ParagraphJustification.CENTER_JUSTIFY;
    if (kind === "r") return ParagraphJustification.RIGHT_JUSTIFY;
    return ParagraphJustification.LEFT_JUSTIFY;
}

/* ---------------- project snapshot / cleanup ---------------- */
/* Reason for existence: we render inside the user's LIVE AE session. If we leave
 * a single comp or footage item behind, his project gets polluted a little bit
 * every run. Purge by id-set difference, and never touch anything that was
 * already there. */
function rtSnapshot() {
    var s = {};
    for (var i = 1; i <= app.project.numItems; i++) {
        try { s["" + app.project.item(i).id] = 1; } catch (e) { }
    }
    return s;
}

function rtPurge(snap) {
    var gone = 0, stuck = [];
    // render queue first: a queued item keeps the comp alive
    try {
        var rq = app.project.renderQueue;
        for (var q = rq.numItems; q >= 1; q--) {
            try { rq.item(q).remove(); } catch (e0) { }
        }
    } catch (e1) { }
    for (var i = app.project.numItems; i >= 1; i--) {
        var it = app.project.item(i);
        if (!it) continue;
        var id = null;
        try { id = "" + it.id; } catch (e2) { continue; }
        if (snap[id] === 1) continue;          // belongs to the user -> hands off
        try { it.remove(); gone++; } catch (e3) { stuck.push(it.name); }
    }
    return { gone: gone, stuck: stuck };
}

/* ---------------- layer helpers ---------------- */
function rtComp(name, w, h, fps, dur) {
    return app.project.items.addComp(name, w, h, 1.0, dur, fps);
}

function rtSolid(comp, color, name, dur, parentLayer) {
    var s = comp.layers.addSolid(c3(color), name, comp.width, comp.height, 1.0,
                                 dur || comp.duration);
    if (parentLayer) s.parent = parentLayer;
    return s;
}

function rtOpa(layer) { return layer.property("ADBE Transform Group").property("ADBE Opacity"); }
function rtPos(layer) { return layer.property("ADBE Transform Group").property("ADBE Position"); }
function rtScale(layer) { return layer.property("ADBE Transform Group").property("ADBE Scale"); }
/* 2D layers rotate through "ADBE Rotate Z"; "ADBE Rotation" only exists on 3D
 * layers and comes back null there (the vinyl failed with "null is not an
 * object" because of exactly this). */
function rtRot(layer) {
    var tg = layer.property("ADBE Transform Group");
    var p = tg.property("ADBE Rotate Z");
    if (!p) p = tg.property("ADBE Rotation");
    return p;
}
function rtAnchor(layer) { return layer.property("ADBE Transform Group").property("ADBE Anchor Point"); }

function rtText(comp, body, opt) {
    opt = opt || {};
    var layer = comp.layers.addText(body || "");
    if (opt.name) layer.name = opt.name;
    var tp = layer.property("ADBE Text Properties").property("ADBE Text Document");
    var td = tp.value;
    td.text = body || "";
    if (opt.size) td.fontSize = opt.size;
    if (opt.font) { try { td.font = opt.font; } catch (eF) { } }
    if (opt.color) td.fillColor = c3(opt.color);
    if (opt.justify) td.justification = rtJustify(opt.justify);
    if (opt.tracking !== undefined && opt.tracking !== null) td.tracking = opt.tracking;
    if (opt.leading !== undefined && opt.leading !== null) td.leading = opt.leading;
    if (opt.bold) td.fauxBold = true;
    if (opt.italic) td.fauxItalic = true;
    tp.setValue(td);
    if (opt.parent) layer.parent = opt.parent;
    if (opt.pos) rtPos(layer).setValue(opt.pos);
    if (opt.anchor) rtAnchor(layer).setValue(opt.anchor);
    return layer;
}

function rtFadeIn(layer, t0, dur, peak) {
    peak = (peak === undefined || peak === null) ? 100 : peak;
    var p = rtOpa(layer);
    p.setValueAtTime(t0, 0);
    p.setValueAtTime(t0 + dur, peak);
}
function rtFadeOut(layer, t0, dur) {
    var p = rtOpa(layer);
    p.setValueAtTime(t0, rtOpa(layer).valueAtTime(t0, false));
    p.setValueAtTime(t0 + dur, 0);
}

/* Fit an image layer inside a box, keeping aspect. Returns the layer or null. */
function rtImage(comp, file, boxW, boxH, opt) {
    opt = opt || {};
    if (!file) return null;
    var f = new File(file);
    if (!f.exists) return null;
    var layer = null;
    try {
        var io = new ImportOptions(f);
        io.importAs = ImportAsType.FOOTAGE;
        var item = app.project.importFile(io);
        layer = comp.layers.add(item);
    } catch (e) { return null; }
    if (!layer) return null;
    var sw = 1, sh = 1;
    try { sw = layer.source.width; sh = layer.source.height; } catch (e2) { }
    // the layer must be scaled to fit the box; both axes use the same factor so
    // the aspect never gets squashed
    // To fit inside the box keeping aspect: one factor for both axes.
    // (`100 * k` because scale is a percentage of the layer's own size.)
    if (!sw || !sh) { sw = boxW; sh = boxH; }
    var kv = rtMin(boxW / sw, boxH / sh);
    rtScale(layer).setValue([kv * 100, kv * 100]);
    if (opt.parent) layer.parent = opt.parent;
    if (opt.pos) rtPos(layer).setValue(opt.pos);
    return layer;
}

function rtAudio(comp, file, start) {
    var f = new File(file);
    if (!f.exists) return null;
    var layer = null;
    try {
        var io = new ImportOptions(f);
        io.importAs = ImportAsType.FOOTAGE;
        var item = app.project.importFile(io);
        layer = comp.layers.add(item);
    } catch (e) { return null; }
    if (!layer) return null;
    if (start) layer.startTime = -start;
    try { layer.moveToEnd(); } catch (e2) { }
    return layer;
}

/* ---------------- audio visualisation effects ----------------
 * Parameter indices (ADBE AudSpect, 24 params) were dumped from THIS AE build
 * by enumerating fx.property(i).name / .matchName and writing them to a file -
 * not guessed from the binary. Localised names in this build (zh_CN):
 *   1 audio layer        2 start point        3 end point       4 path
 *   5 use polar path     6 start freq         7 end freq        8 bands
 *   9 max height        10 audio duration ms 11 audio offset ms 12 thickness
 *  13 softness          14 inner color       15 outer color
 *  16 blend overlay colors                   17 hue interpolation
 *  18 dynamic hue       19 color symmetry    20 display options
 *  21 side options      22 duration averaging
 *  23 composite on original                  24 composite options
 * ADBE AudWave has 17; its 12/13 are inner/outer color (not 14/15).
 * Getting one of these wrong silently produces a black layer - there is no
 * error, the spectrum just never shows up. So they are set defensively with
 * try/catch per property and the failures are reported back.
 */
function rtSpectrum(comp, audioLayer, opt) {
    opt = opt || {};
    var solid = comp.layers.addSolid([0, 0, 0], opt.name || "spectrum",
                                     comp.width, comp.height, 1.0, comp.duration);
    try { solid.blendingMode = BlendingMode.ADD; } catch (eB) { }
    var errs = [];
    var fx = null;
    try {
        fx = solid.property("ADBE Effect Parade").addProperty("ADBE AudSpect");
    } catch (e0) { return { layer: solid, fx: null, errs: ["cannot attach AudSpect"] }; }
    if (audioLayer) {
        try { fx.property(1).setValue(audioLayer.index); } catch (e1) { errs.push("1"); }
    }
    /* Polar mode needs an actual PATH to wrap around. With "Path: none" AE draws
     * a flat strip and the polar flag does nothing - the ring came out empty.
     * So when pathMask is asked for we put a circle mask on the spectrum layer
     * itself and point the effect's path at mask #1. */
    if (opt.pathMask) {
        var pc = opt.pathCenter || [comp.width / 2, comp.height / 2];
        var pr = opt.pathR || Math.min(comp.width, comp.height) * 0.36;
        rtCircleMask(solid, pc[0], pc[1], pr);
        try { fx.property(4).setValue(1); } catch (eP) { errs.push("path"); }
    }
    /* The start/end POINTS are not decoration, and they matter in BOTH modes:
     * left at their default (0,0) the spectrum collapses to a degenerate line -
     * a flat row of dots in flat mode, and a single tiny arrow at the path's
     * origin in polar mode. Neither reports an error. */
    var p0 = opt.p0 || [0, comp.height / 2];
    var p1 = opt.p1 || [comp.width, comp.height / 2];
    try { fx.property(2).setValue(p0); } catch (eA2) { errs.push("2"); }
    try { fx.property(3).setValue(p1); } catch (eA3) { errs.push("3"); }
    var sets = [
        [5, opt.polar ? 1 : 0],
        [6, opt.fStart === undefined ? 32 : opt.fStart],
        [7, opt.fEnd === undefined ? 14000 : opt.fEnd],
        [8, opt.bands === undefined ? 48 : opt.bands],
        [9, opt.maxH === undefined ? 420 : opt.maxH],
        [10, opt.ms === undefined ? 75 : opt.ms],
        [12, opt.thick === undefined ? 5 : opt.thick],
        [13, opt.soft === undefined ? 45 : opt.soft],
        [16, opt.blend === undefined ? 1 : opt.blend],
        [18, opt.dynHue === undefined ? 1 : opt.dynHue],
        [19, opt.sym === undefined ? 1 : opt.sym],
        [20, opt.display === undefined ? 1 : opt.display],
        [21, opt.side === undefined ? 3 : opt.side],
        [22, opt.avg === undefined ? 0 : opt.avg],
        [23, opt.onOriginal === undefined ? 1 : opt.onOriginal]
    ];
    for (var i = 0; i < sets.length; i++) {
        try { fx.property(sets[i][0]).setValue(sets[i][1]); } catch (e2) { errs.push("" + sets[i][0]); }
    }
    // colors are COLOR type -> need 4 components
    if (!rtSetColor(fx.property(14), c3(opt.inner || [0.35, 0.85, 1]))) errs.push("14");
    if (!rtSetColor(fx.property(15), c3(opt.outer || [0.15, 0.25, 0.75]))) errs.push("15");
    return { layer: solid, fx: fx, errs: errs };
}

function rtWave(comp, audioLayer, opt) {
    opt = opt || {};
    var solid = comp.layers.addSolid([0, 0, 0], opt.name || "waveform",
                                     comp.width, comp.height, 1.0, comp.duration);
    try { solid.blendingMode = BlendingMode.ADD; } catch (eB) { }
    var fx = null;
    try { fx = solid.property("ADBE Effect Parade").addProperty("ADBE AudWave"); }
    catch (e0) { return { layer: solid, fx: null }; }
    if (audioLayer) { try { fx.property(1).setValue(audioLayer.index); } catch (e1) { } }
    var sets = [[5, opt.samples === undefined ? 250 : opt.samples],
                [6, opt.maxH === undefined ? 260 : opt.maxH],
                [7, opt.ms === undefined ? 90 : opt.ms],
                [9, opt.thick === undefined ? 3 : opt.thick],
                [10, opt.soft === undefined ? 40 : opt.soft],
                [14, opt.waveOpt === undefined ? 0 : opt.waveOpt],
                [15, opt.display === undefined ? 0 : opt.display],
                [16, 1]];
    for (var i = 0; i < sets.length; i++) {
        try { fx.property(sets[i][0]).setValue(sets[i][1]); } catch (e2) { }
    }
    rtSetColor(fx.property(12), c3(opt.inner || [0.4, 0.9, 1]));
    rtSetColor(fx.property(13), c3(opt.outer || [0.2, 0.3, 0.8]));
    return { layer: solid, fx: fx };
}

/* Re-bind every Audio Spectrum / Waveform effect to the audio layer.
 *
 * CORRECTION 2026-10-25 (the old text below this line used to be wrong, and it
 * was misleading - do not reason from the old claim again):
 *   property(1) really IS the audio layer (verified by dumping all 24 param
 *   names out of this AE build). But it is NOT a snapshot index that goes
 *   stale: AE moves it along when layers are inserted --
 *       add 1st text layer: audioIndex 2 -> 3, property(1) reads 2 -> 3
 *       add 2nd text layer: audioIndex 3 -> 4, property(1) reads 3 -> 4
 *   Removing this rebind entirely (driver sets RES.bound = 0 instead) produced
 *   pixel readings IDENTICAL to the good build on all three audio templates
 *   (left-strip motion bars 10.90 / ring 9.57 / vertical 15.97).
 *   => the original "flat row of dots, amplitude always 0" was caused by the
 *      MUTED shadow audio track and by the 45..16000 Hz range (AE's frequency
 *      axis is LINEAR, so the voice energy sits in the leftmost few bands),
 *      NOT by a stale index. Crediting this function for that fix is wrong.
 *
 * So why keep it? Purely as a seat belt: it still helps if the build order ever
 * changes (effect created before the audio layer exists). Cost is one pass over
 * the layers.
 * And note: `bound` in the receipt only proves THIS STEP RAN. It does not
 * prove the picture is right - the gate says exactly that, do not read more
 * into it.
 */
function rtBindAudio(comp, audioLayer) {
    if (!comp || !audioLayer) return 0;
    var bound = 0;
    for (var i = 1; i <= comp.numLayers; i++) {
        var ly = comp.layer(i);
        var parade = null;
        try { parade = ly.property("ADBE Effect Parade"); } catch (e0) { continue; }
        if (!parade) continue;
        for (var e = 1; e <= parade.numProperties; e++) {
            var fx = parade.property(e);
            var mn = "";
            try { mn = fx.matchName; } catch (e1) { continue; }
            if (mn === "ADBE AudSpect" || mn === "ADBE AudWave") {
                try { fx.property(1).setValue(audioLayer.index); bound++; } catch (e2) { }
            }
        }
    }
    return bound;
}

/* Circle mask in LAYER-space coordinates.
 * Image layers are the trap here: their masks are expressed in the source
 * pixel grid (0..source.width), NOT in comp coordinates. Passing comp
 * coordinates puts the circle far outside the layer and the layer simply
 * disappears - which is how the vinyl disc and the singer chips first died. */
function rtCircleMask(layer, cx, cy, r) {
    try {
        var m = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
        m.name = "circle";
        var shp = new Shape();
        var pts = [], tI = [], tO = [];
        for (var i = 0; i < 20; i++) {
            var ang = (Math.PI * 2 * i) / 20;
            pts.push([cx + Math.cos(ang) * r, cy + Math.sin(ang) * r]);
            tI.push([0, 0]); tO.push([0, 0]);
        }
        shp.vertices = pts; shp.inTangents = tI; shp.outTangents = tO;
        shp.closed = true;
        m.property("ADBE Mask Shape").setValue(shp);
        try { m.property("ADBE Mask Mode").setValue(MaskMode.ADD); } catch (eMode) { }
        return true;
    } catch (e) { return false; }
}

function rtSourceSize(layer) {
    var w = 0, h = 0;
    try { w = layer.source.width; h = layer.source.height; } catch (e) { }
    return [w, h];
}

/* A soft radial glow: big solid + ellipse mask + blur + low opacity.
 * Cheap, no plugin risk, and it is what makes a flat background look "designed". */
/* Every template starts the same way: make the comp, hang the audio on it,
 * lay down the base colour. Doing it once here keeps the 6 templates honest -
 * a template that forgets the audio layer used to produce a silent video with
 * no error at all, which is exactly the kind of silent failure we hate. */
function rtBaseScene(CTX, name, opt) {
    opt = opt || {};
    var comp = rtComp(name, CTX.w, CTX.h, CTX.fps, CTX.dur);
    /* Only ONE audio layer ever goes into the comp.
     * If a loudness-flattened twin exists we use it here (so the spectrum sees
     * full-scale levels) and put the real track back with ffmpeg after the
     * render. Muting the twin instead would silently starve the effect - the
     * bars collapse to a flat row of dots and nothing reports an error. */
    var useSpec = (CTX.specAudio && CTX.specAudio.length > 0);
    var audio = rtAudio(comp, useSpec ? CTX.specAudio : CTX.audio, CTX.trim);
    RT.audioLayer = audio;
    RT.usedSpecAudio = !!useSpec;
    if (!audio) {
        RT.missingAudio = true;      // driver reports this, do NOT silently render
    }
    var base = rtSolid(comp, opt.base || CTX.bg1 || [0.04, 0.05, 0.09], "base", CTX.dur);
    return { comp: comp, audio: audio, base: base };
}

function rtGlowBlob(comp, color, cx, cy, radius, opacity) {
    var s = comp.layers.addSolid(color, "glow", comp.width, comp.height, 1.0, comp.duration);
    var m = null;
    try {
        m = s.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
        m.name = "ellipse";
        var shape = new Shape();
        var pts = [], tan = [], tanIn = [];
        var N = 12, i, ang, x, y;
        for (i = 0; i < N; i++) {
            ang = (Math.PI * 2 * i) / N;
            x = cx + Math.cos(ang) * radius;
            y = cy + Math.sin(ang) * radius * 0.92;
            pts.push([x, y]);
            tan.push([0, 0]);
            tanIn.push([0, 0]);
        }
        shape.vertices = pts;
        shape.inTangents = tanIn;
        shape.outTangents = tan;
        shape.closed = true;
        m.property("ADBE Mask Shape").setValue(shape);
        m.property("ADBE Mask Feather").setValue([radius * 0.85, radius * 0.85]);
        m.property("ADBE Mask Mode").setValue(MaskMode.ADD);
    } catch (e0) { }
    rtOpa(s).setValue(opacity === undefined ? 40 : opacity);
    return s;
}

/* Darken a picture instead of fading it out.
 *
 * Lowering OPACITY is the obvious way to make somebody look inactive and it is
 * the wrong one: the layer turns see-through and you end up watching the
 * backdrop through the singer's face. What the reference clips do is leave the
 * pixels solid and just unlit - brightness down, contrast down.
 *
 * ADBE Brightness & Contrast 2 (dumped from this build): 1 = brightness,
 * 2 = contrast, 3 = legacy HDR, 4 = composite options. Both 1 and 2 are
 * animatable, so the same pair drives the two states.
 *
 * Returns {bri, con} or null when the effect cannot be added - the caller has
 * to fall back to opacity then, it must not assume this worked.
 */
function rtDim(layer, brightness, contrast) {
    try {
        var fx = layer.property("ADBE Effect Parade")
                      .addProperty("ADBE Brightness & Contrast 2");
        fx.property(1).setValue(brightness === undefined ? -85 : brightness);
        fx.property(2).setValue(contrast === undefined ? -30 : contrast);
        return { bri: fx.property(1), con: fx.property(2) };
    } catch (e) {
        return null;
    }
}
