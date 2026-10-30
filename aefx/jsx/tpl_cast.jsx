/* ============================================================================
 * TEMPLATE 3 / cast - multi-singer stage ("the pictures sit dark, whoever
 * sings lights up")
 *
 * The look people mean when they say "dark by default, the singer lights up":
 * a row of portraits along the bottom. At rest every one of them is DARK -
 * not see-through, dark: brightness pulled down, contrast pulled down, so the
 * face is still there, only unlit. When a singer takes a line his portrait
 * comes up to full brightness, lifts a little, grows, and picks up a coloured
 * rim + halo in his own colour. When he stops, it drops back into the dark.
 *
 * Why brightness and not opacity: fading the layer to 30% makes the portrait
 * transparent, so you end up watching the background through somebody's face.
 * Dimming the pixels keeps it solid and just unlit, which is what the
 * reference clips actually do.
 *
 * Needs CTX.cast = [[start, end, name, colour]] (portrait path optional as the
 * 5th field). Without it we fall back to one singer and keep him lit for the
 * whole song - a solo singer is singing the whole time, so "nobody is lit"
 * would be wrong, not neutral.
 * ==========================================================================*/
function TPL_build(CTX) {
    var S = rtBaseScene(CTX, "VS_cast", { base: CTX.bg1 || [0.035, 0.04, 0.075] });
    var comp = S.comp, AL = S.audio;
    var W = CTX.w, H = CTX.h, DUR = CTX.dur;
    var lines = CTX.lines || [];
    var cast = CTX.cast || [];

    /* --- soft round mask (halo needs feather, the plain chip does not) --- */
    function roundMask(layer, cx, cy, r, feather) {
        try {
            var m = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
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
            m.property("ADBE Mask Mode").setValue(MaskMode.ADD);
            if (feather > 0) {
                m.property("ADBE Mask Feather").setValue([feather, feather]);
            }
            return m;
        } catch (e0) { return null; }
    }
    /* A solid disc that lives at (cx, cy).
     * Anchor AND position must both move to that point - moving the anchor
     * alone shifts the layer by (anchor - position), which once collapsed all
     * six portraits into the middle of the frame. */
    function disc(name, color, cx, cy, r, feather) {
        var L = rtSolid(comp, color, name, DUR);
        roundMask(L, cx, cy, r, feather || 0);
        rtAnchor(L).setValue([cx, cy]);
        rtPos(L).setValue([cx, cy]);
        return L;
    }

    /* --- collect the cast once, in first-appearance order --- */
    var people = [], seen = {};
    for (var c = 0; c < cast.length; c++) {
        var nm = cast[c][2];
        if (seen[nm]) continue;
        seen[nm] = 1;
        people.push({ name: nm, color: cast[c][3] || CTX.accent || [1, 1, 1],
                      avatar: cast[c][4] || "",
                      /* field 6 = [dim, lit]: how far to darken THIS picture
                       * and how far to raise it when he sings, both measured
                       * on the Python side (ae_build._dim_for_avatar).
                       * AE brightness is additive, so one fixed value leaves
                       * bright portraits looking untouched and crushes dark
                       * ones into the background - and a dark portrait that
                       * is simply "not dimmed" still cannot compete with a
                       * bright one, which is why the lit side is measured too.
                       * Not present -> fall back to -85 / +8. */
                      dim: (cast[c][5] && cast[c][5].length >= 1)
                           ? cast[c][5][0] : null,
                      lit: (cast[c][5] && cast[c][5].length >= 2)
                           ? cast[c][5][1] : null });
    }
    if (!people.length) {
        people.push({ name: CTX.title || "solo", color: CTX.accent || [1, 1, 1],
                      avatar: CTX.cover || "", dim: null });
    }

    /* --- backdrop: one tinted block per contiguous run, cross-faded --- */
    var runIdx = -1, last = null;
    for (var k = 0; k < cast.length; k++) {
        var e = cast[k];
        var key = e[2];
        if (key !== last) {
            var until = e[1];
            for (var q = k + 1; q < cast.length; q++) {
                if (cast[q][2] !== key) { until = cast[q][0]; break; }
                until = cast[q][1];
                if (cast[q + 1] && cast[q + 1][2] !== key) break;
            }
            until = rtMin(until, DUR);
            var blk = rtSolid(comp, [e[3][0] * 0.5, e[3][1] * 0.5, e[3][2] * 0.5],
                             "bgRun" + (++runIdx), DUR);
            var op = rtOpa(blk);
            op.setValueAtTime(0, 0);
            var fade = rtMin(0.5, (until - e[0]) / 3);
            if (e[0] > 0.02) op.setValueAtTime(Math.max(0, e[0] - fade), 0);
            op.setValueAtTime(e[0], 55);
            op.setValueAtTime(Math.max(e[0] + 0.05, until - fade), 55);
            op.setValueAtTime(until, 0);
            op.setValueAtTime(DUR, 0);
            last = key;
        }
    }

    rtGlowBlob(comp, CTX.accent || [0.4, 0.3, 0.8], W * 0.5, H * 0.34, W * 0.5, 18);

    /* --- geometry: keep the row inside the frame no matter how many singers.
     * The disc must survive being scaled up 1.16x without touching its
     * neighbour, so the radius is derived from the step, not the other way
     * round (2 * r * 1.16 has to stay under one step). --- */
    var n = people.length;
    var step = rtMin(W * 0.92 / Math.max(n, 1), W * 0.26);
    var r = rtMin(step * 0.40, H * 0.135);
    var startX = W / 2 - (step * (n - 1)) / 2;
    var chipY = H * 0.74;
    var lift = H * 0.014;          // how far the active portrait rises
    var nameY = chipY + r * 1.30 + H * 0.020;

    /* One bucket per singer: every animatable property of his portrait goes in
     * here, and the pass below just replays his sung intervals onto his own
     * bucket. Keeping the props inside the person they belong to is what stops
     * the classic "wrong person lights up" bug - that happens when properties
     * are collected into one flat list and indexed later. */
    var PV = [];
    for (var pi = 0; pi < n; pi++) {
        PV.push({ name: people[pi].name, props: [], covered: false });
    }

    for (var p = 0; p < n; p++) {
        var pv = people[p];
        var px = startX + step * p;
        var col = pv.color;

        /* halo (bottom) -> rim (middle) -> portrait (top) */
        var ring = disc("halo" + p, col, px, chipY, r * 1.26, r * 0.62);
        rtOpa(ring).setValue(0);
        var rim = disc("rim" + p, [rtMin(1, col[0] * 0.55 + 0.45),
                                   rtMin(1, col[1] * 0.55 + 0.45),
                                   rtMin(1, col[2] * 0.55 + 0.45)],
                       px, chipY, r * 1.10, 0);
        rtOpa(rim).setValue(0);
        var chip = disc("chip" + p, col, px, chipY, r, 0);

        var body = chip, baseSc = [100, 100];
        var hasPic = false;
        if (pv.avatar) {
            var pic = rtImage(comp, pv.avatar, r * 2, r * 2, {});
            if (pic) {
                hasPic = true;
                body = pic;
                rtPos(pic).setValue([px, chipY]);
                var psz = rtSourceSize(pic);
                rtCircleMask(pic, psz[0] / 2, psz[1] / 2,
                             Math.min(psz[0], psz[1]) * 0.49);
                /* rtImage already scaled the layer to fit the box, so the rest
                 * state is its fitted scale - writing 100 here would make the
                 * portrait jump to full size the moment it starts. */
                var sv = rtScale(pic).value;
                baseSc = [sv[0], sv[1]];
                /* the disc underneath keeps the row readable if the picture has
                 * transparent corners - it rides along with the same dimming */
                PV[p].props.push([rtOpa(chip), 50, 100]);
                PV[p].props.push([rtScale(chip), [95, 95], [113, 113]]);
                PV[p].props.push([rtPos(chip), [px, chipY + lift * 0.30],
                                  [px, chipY - lift]]);
            }
        }

        /* THE dimming. This is the whole point of the template.
         * dv is per-picture (measured by ae_build._dim_for_avatar); without a
         * measurement we fall back to -85 / -30. The lit state goes slightly
         * ABOVE neutral (+8 brightness, +5 contrast) so the singer does not
         * just come back to normal - he comes up past everybody else. */
        var dv = (pv.dim === null || pv.dim === undefined) ? -85 : pv.dim;
        var lv = (pv.lit === null || pv.lit === undefined) ? 8 : pv.lit;
        var dc = Math.round(dv * 0.35);
        var dim = rtDim(body, dv, dc);
        if (dim) {
            PV[p].props.push([dim.bri, dv, lv]);
            PV[p].props.push([dim.con, dc, 5]);
        } else {
            PV[p].props.push([rtOpa(body), 42, 100]);   // effect missing -> fade
        }
        /* Size carries as much of the signal as brightness does: at rest the
         * portrait sits slightly SMALL and low, singing it grows and rises.
         * A pure brightness change alone reads as "the video got darker",
         * the size step is what makes it read as "this one is singing". */
        PV[p].props.push([rtScale(body), [baseSc[0] * 0.95, baseSc[1] * 0.95],
                          [baseSc[0] * 1.13, baseSc[1] * 1.13]]);
        PV[p].props.push([rtPos(body), [px, chipY + lift * 0.30],
                          [px, chipY - lift]]);
        PV[p].props.push([rtOpa(rim), 0, 92]);
        PV[p].props.push([rtScale(rim), [95, 95], [113, 113]]);
        PV[p].props.push([rtPos(rim), [px, chipY + lift * 0.30],
                          [px, chipY - lift]]);
        PV[p].props.push([rtOpa(ring), 0, 66]);
        PV[p].props.push([rtScale(ring), [95, 95], [118, 118]]);
        PV[p].props.push([rtPos(ring), [px, chipY + lift * 0.30],
                          [px, chipY - lift]]);
    }

    /* names last, so they sit above everybody's halo instead of under it */
    for (var np = 0; np < n; np++) {
        var npx = startX + step * np;
        var nmL = rtText(comp, people[np].name, {
            name: "who" + np, size: Math.round(H * 0.029), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [npx, nameY]
        });
        nmL.startTime = 0;
        PV[np].props.push([rtOpa(nmL), 38, 100]);
        PV[np].props.push([rtPos(nmL), [npx, nameY], [npx, nameY - lift * 0.6]]);
    }

    /* light each person up exactly while they are singing */
    for (var w = 0; w < cast.length; w++) {
        var seg = cast[w];
        var who = seg[2];
        for (var pp = 0; pp < PV.length; pp++) {
            if (PV[pp].name !== who) continue;
            PV[pp].covered = true;
            var a0 = rtClamp(seg[0], 0, DUR - 0.05);
            var b0 = rtClamp(seg[1], a0 + 0.05, DUR);
            for (var ai = 0; ai < PV[pp].props.length; ai++) {
                var rec = PV[pp].props[ai];
                var prop = rec[0], lo = rec[1], hi = rec[2];
                prop.setValueAtTime(Math.max(0, a0 - 0.16), lo);
                prop.setValueAtTime(a0, hi);
                prop.setValueAtTime(b0, hi);
                prop.setValueAtTime(rtMin(DUR, b0 + 0.16), lo);
            }
        }
    }
    /* No cast table at all -> one singer, and he sings the whole song.
     * Leaving him dark would look like the template is broken. */
    if (!cast.length) {
        for (var sp = 0; sp < PV.length; sp++) {
            for (var si = 0; si < PV[sp].props.length; si++) {
                PV[sp].props[si][0].setValue(PV[sp].props[si][2]);
            }
        }
    }

    /* --- lyrics, tinted by whoever sings them --- */
    var fs = Math.round(H * 0.058);
    var subFS = Math.round(H * 0.030);
    var cy = H * 0.42;
    for (var i = 0; i < lines.length; i++) {
        var ln = lines[i];
        var a = rtClamp(ln[0], 0, DUR - 0.05);
        var b = rtClamp(ln[1], a + 0.2, DUR);
        var main = ln[3] ? ln[3] : ln[2];
        var sub = ln[3] ? ln[2] : "";
        var col2 = ln[4] || CTX.accent || [1, 1, 1];
        var L = rtText(comp, main, {
            name: "ln" + i, size: fs, font: CTX.font, color: [1, 1, 1, 1],
            justify: "c", pos: [W / 2, sub ? cy - H * 0.022 : cy]
        });
        L.startTime = 0;
        L.inPoint = a; L.outPoint = b;
        rtFadeIn(L, a, 0.16, 100);
        rtOpa(L).setValueAtTime(b - 0.15, 100);
        rtOpa(L).setValueAtTime(b, 0);
        if (sub) {
            var SL = rtText(comp, sub, {
                name: "sub" + i, size: subFS, font: CTX.font,
                color: [col2[0], col2[1], col2[2], 1], justify: "c",
                pos: [W / 2, cy + H * 0.048]
            });
            SL.startTime = 0;
            SL.inPoint = a; SL.outPoint = b;
            rtFadeIn(SL, a + 0.05, 0.2, 76);
            rtOpa(SL).setValueAtTime(b - 0.15, 76);
            rtOpa(SL).setValueAtTime(b, 0);
        }
    }

    if (CTX.title) {
        var hd = rtText(comp, CTX.title, {
            name: "title", size: Math.round(H * 0.036), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.075], bold: true
        });
        hd.startTime = 0;
        rtOpa(hd).setValue(88);
    }
    if (CTX.subtitle) {
        var sb = rtText(comp, CTX.subtitle, {
            name: "subtitle", size: Math.round(H * 0.024), font: CTX.font,
            color: [1, 1, 1, 1], justify: "c", pos: [W / 2, H * 0.075 + H * 0.042]
        });
        sb.startTime = 0;
        rtOpa(sb).setValue(58);
    }
    return comp;
}
