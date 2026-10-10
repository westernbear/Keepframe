/* Keepframe host: fixed entry points, JSON data only, ExtendScript ES3. */
/* ExtendScript is ES3: there is no built-in JSON, and without it every bridge
 * read fails quietly.  Minimal parse/stringify after json2.js (public domain),
 * installed only when the host lacks them. */
if (typeof JSON !== "object" || JSON === null) {
    JSON = {};
}
(function (json) {
    var ESCAPES = { "\"": "\\\"", "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t" };
    var UNSAFE = /[\\"\u0000-\u001f\u007f-\u009f\u00ad\u0600-\u0604\u070f\u17b4\u17b5\u200c-\u200f\u2028-\u202f\u2060-\u206f\ufeff\ufff0-\uffff]/g;
    var DANGEROUS = /[\u0000\u00ad\u0600-\u0604\u070f\u17b4\u17b5\u200c-\u200f\u2028-\u202f\u2060-\u206f\ufeff\ufff0-\uffff]/g;
    var STRUCTURE = /^[\],:{}\s]*$/;
    var ESCAPE_SEQ = /\\(?:["\\\/bfnrt]|u[0-9a-fA-F]{4})/g;
    var TOKENS = /"[^"\\\n\r]*"|true|false|null|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?/g;
    var OPEN_BRACKETS = /(?:^|:|,)(?:\s*\[)+/g;

    function hex(c) {
        return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
    }

    function quote(text) {
        return "\"" + text.replace(UNSAFE, function (c) { return ESCAPES[c] || hex(c); }) + "\"";
    }

    function encode(value) {
        var parts = [];
        var key;
        var index;
        var item;
        if (value === null) { return "null"; }
        switch (typeof value) {
            case "string": return quote(value);
            case "number": return isFinite(value) ? String(value) : "null";
            case "boolean": return String(value);
            case "object":
                if (Object.prototype.toString.call(value) === "[object Array]") {
                    for (index = 0; index < value.length; index += 1) {
                        item = encode(value[index]);
                        parts.push(item === undefined ? "null" : item);
                    }
                    return "[" + parts.join(",") + "]";
                }
                for (key in value) {
                    if (Object.prototype.hasOwnProperty.call(value, key)) {
                        item = encode(value[key]);
                        if (item !== undefined) { parts.push(quote(String(key)) + ":" + item); }
                    }
                }
                return "{" + parts.join(",") + "}";
            default:
                return undefined;
        }
    }

    if (typeof json.stringify !== "function") {
        json.stringify = function (value) { return encode(value); };
    }
    if (typeof json.parse !== "function") {
        json.parse = function (text) {
            var source = String(text).replace(DANGEROUS, hex);
            if (STRUCTURE.test(source.replace(ESCAPE_SEQ, "@").replace(TOKENS, "]").replace(OPEN_BRACKETS, ""))) {
                return eval("(" + source + ")");
            }
            throw new SyntaxError("JSON.parse");
        };
    }
}(JSON));

(function (global) {
    var HOST_BUILD = "dev";
    var PROP_NAMES = ["position_x", "position_y", "scale", "rotation", "opacity"];
    var PROP_MATCHES = ["ADBE Position_0", "ADBE Position_1", "ADBE Scale", "ADBE Rotate Z", "ADBE Opacity"];

    function own(object, key) {
        return Object.prototype.hasOwnProperty.call(object, key);
    }

    function array(value) {
        return Object.prototype.toString.call(value) === "[object Array]";
    }

    function requireValue(condition, message) {
        if (!condition) { throw new Error(message); }
    }

    function number(value) {
        return typeof value === "number" && isFinite(value);
    }

    function rgb(value) {
        requireValue(typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value), "invalid RGB color");
        return [parseInt(value.substr(1, 2), 16) / 255, parseInt(value.substr(3, 2), 16) / 255,
            parseInt(value.substr(5, 2), 16) / 255];
    }

    function validateKeys(keys, dimensions) {
        var i, j, k, side, value;
        requireValue(array(keys) && keys.length > 0, "invalid property keys");
        for (i = 0; i < keys.length; i += 1) {
            k = keys[i];
            requireValue(array(k) && k.length === 4 && number(k[0]) && k[0] >= 0
                && (i === 0 || k[0] > keys[i - 1][0]), "invalid key frame");
            value = dimensions === 1 ? [k[1]] : k[1];
            requireValue(array(value) && value.length === dimensions, "invalid key dimensions");
            for (j = 0; j < dimensions; j += 1) {
                requireValue(number(value[j]), "invalid key value");
            }
            for (side = 2; side <= 3; side += 1) {
                if (k[side] === null) { continue; }
                requireValue(array(k[side]) && k[side].length === dimensions, "invalid ease dimensions");
                for (j = 0; j < dimensions; j += 1) {
                    value = k[side][j];
                    requireValue(array(value) && value.length === 2 && number(value[0])
                        && value[0] >= 0.1 && value[0] <= 100 && number(value[1]), "invalid temporal ease");
                }
            }
        }
    }

    function optional(value) { return value === undefined ? null : value; }

    function styledText(s) { return !!s && s.kind === "text" && s.source.tracking !== undefined; }

    function validateColorKeys(keys) {
        var i, j;
        validateKeys(keys, 4);
        for (i = 0; i < keys.length; i += 1) {
            for (j = 0; j < 4; j += 1) { requireValue(keys[i][1][j] >= 0 && keys[i][1][j] <= 1, "invalid effect color"); }
        }
    }

    function validateRamp(ramp) {
        requireValue(ramp && (ramp.shape === 1 || ramp.shape === 2), "invalid ramp effect");
        validateKeys(ramp.start, 2); validateKeys(ramp.end, 2);
        validateColorKeys(ramp.start_color); validateColorKeys(ramp.end_color);
    }

    function validateStyle(s) {
        var i, shadow, stroke, shadows = optional(s.effects.shadows);
        if (styledText(s)) {
            stroke = optional(s.source.stroke);
            requireValue(number(s.source.tracking) && (stroke === null || (number(stroke.width) && stroke.width > 0
                && typeof stroke.over_fill === "boolean")), "invalid text style");
            if (stroke !== null) { rgb(stroke.color); }
        }
        requireValue(shadows === null || (s.kind === "text" && array(shadows)), "invalid shadow effects");
        for (i = 0; shadows !== null && i < shadows.length; i += 1) {
            shadow = shadows[i];
            requireValue(shadow && number(shadow.opacity) && shadow.opacity >= 0 && shadow.opacity <= 100
                && number(shadow.direction) && number(shadow.distance) && shadow.distance >= 0
                && number(shadow.softness) && shadow.softness >= 0, "invalid shadow effects");
            validateColorKeys([[0, shadow.color, null, null]]);
        }
        if (optional(s.effects.fill) !== null) {
            requireValue(s.kind === "text", "invalid fill effect");
            validateRamp(s.effects.fill);
        }
        if (optional(s.effects.gradient) !== null) { validateRamp(s.effects.gradient); }
    }

    function validate(spec, assets, force) {
        var i, j, s, a, ids = {}, names = {};
        requireValue(spec && spec.schema === "keepframe.ae-comp/1" && spec.comp
            && typeof spec.project === "string" && typeof spec.comp.tag === "string"
            && typeof spec.comp.name === "string", "invalid comp spec");
        requireValue(force === "true" || force === "false", "invalid force flag");
        requireValue(assets && typeof assets === "object" && !array(assets), "invalid assets map");
        requireValue(number(spec.comp.width) && spec.comp.width > 0 && Math.floor(spec.comp.width) === spec.comp.width
            && number(spec.comp.height) && spec.comp.height > 0 && Math.floor(spec.comp.height) === spec.comp.height
            && number(spec.comp.fps) && spec.comp.fps > 0
            && number(spec.comp.frames) && spec.comp.frames > 0 && Math.floor(spec.comp.frames) === spec.comp.frames, "invalid comp settings");
        requireValue(array(spec.assets) && array(spec.layers) && array(spec.warnings), "invalid spec lists");
        for (i = 0; i < spec.assets.length; i += 1) {
            a = spec.assets[i];
            requireValue(a && typeof a.name === "string" && /^[0-9a-f]{64}$/.test(a.sha256), "invalid asset");
            requireValue(own(assets, a.name) && typeof assets[a.name] === "string" && assets[a.name] !== "",
                "asset " + a.name + " was not downloaded");
            requireValue(new File(assets[a.name]).exists, "asset " + a.name + " file does not exist");
            names["$" + a.name] = true;
        }
        for (i = 0; i < spec.layers.length; i += 1) {
            s = spec.layers[i];
            requireValue(s && typeof s.id === "string" && s.id !== "" && !own(ids, "$" + s.id), "invalid layer id");
            ids["$" + s.id] = true;
            requireValue(/^(text|solid|null|image|model|footage)$/.test(s.kind) && typeof s.name === "string"
                && (s.hidden === undefined || typeof s.hidden === "boolean")
                && number(s.order) && number(s["in"]) && number(s.out) && s.out >= s["in"]
                && (s.label === null || (number(s.label) && s.label >= 0 && s.label <= 16
                    && Math.floor(s.label) === s.label)), "invalid layer settings");
            requireValue(s.props && s.effects && array(s.warnings), "invalid layer properties");
            for (j = 0; j < PROP_NAMES.length; j += 1) {
                validateKeys(s.props[PROP_NAMES[j]], j === 2 ? 2 : 1);
            }
            if (s.kind === "text") {
                requireValue(s.source && typeof s.source.text === "string" && s.source.font
                    && typeof s.source.font.family === "string"
                    && (s.source.font.postscript === null || typeof s.source.font.postscript === "string")
                    && number(s.source.size_px) && s.source.size_px > 0
                    && array(s.source.box) && s.source.box.length === 2
                    && number(s.source.box[0]) && s.source.box[0] > 0
                    && number(s.source.box[1]) && s.source.box[1] > 0
                    && array(s.source.anchor_fraction) && s.source.anchor_fraction.length === 2
                    && number(s.source.anchor_fraction[0]) && number(s.source.anchor_fraction[1]), "invalid text source");
                rgb(s.source.color);
            } else {
                requireValue(array(s.anchor) && s.anchor.length === 2
                    && number(s.anchor[0]) && number(s.anchor[1]), "invalid layer anchor");
            }
            if (s.kind === "solid") { rgb(s.source.color); }
            if (s.kind === "image" || s.kind === "model" || s.kind === "footage") {
                requireValue(s.source && own(names, "$" + s.source.asset), "invalid layer asset");
            }
            if (s.kind === "footage") {
                requireValue(number(s.source.start_time) && s.source.start_time >= 0, "invalid footage start");
                requireValue(s.source.stretch === undefined
                    || (number(s.source.stretch) && s.source.stretch > 0 && s.source.stretch <= 9900), "invalid footage stretch");
            }
            if (s.kind === "model") {
                requireValue(array(s.source.fit_box) && s.source.fit_box.length === 2
                    && number(s.source.fit_box[0]) && s.source.fit_box[0] > 0
                    && number(s.source.fit_box[1]) && s.source.fit_box[1] > 0, "invalid model fit box");
                validateKeys(s.props.rotation_x, 1);
                validateKeys(s.props.rotation_y, 1);
            }
            if (s.effects.reveal !== null) {
                validateKeys(s.effects.reveal.completion, 1);
                requireValue(number(s.effects.reveal.angle) && number(s.effects.reveal.feather), "invalid reveal effect");
            }
            if (s.effects.skew !== null) {
                validateKeys(s.effects.skew.skew, 1);
                requireValue(number(s.effects.skew.axis) && (s.effects.skew.baseline_shear === undefined
                    || number(s.effects.skew.baseline_shear)), "invalid skew effect");
            }
            validateStyle(s);
        }
    }

    function hash(text) {
        var h = 2166136261, i;
        for (i = 0; i < text.length; i += 1) {
            h ^= text.charCodeAt(i);
            // Shift/add keeps the FNV multiplication exact in ES3's doubles.
            h = (h + (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24)) >>> 0;
        }
        return ("00000000" + h.toString(16)).slice(-8);
    }

    function layerHash(s, timings) {
        var content = {}, key, started = new Date().getTime(), value;
        // Stack order is handled separately; renumbering must not rewrite layer content.
        for (key in s) { if (own(s, key) && key !== "order" && key !== "name" && key !== "label") { content[key] = s[key]; } }
        // Reapply model placement once to layers synced with the old origin/100% fit.
        if (s.kind === "model") { content.model_fit = "bounds-center-0.9"; }
        value = hash(JSON.stringify(content));
        if (timings) { timings.hash += new Date().getTime() - started; }
        return value;
    }

    function transform(layer) { return layer.property("ADBE Transform Group"); }

    function owned(effect) {
        var name = effect.name, match = effect.matchName;
        return (name === "Keepframe Reveal" && match === "ADBE Linear Wipe")
            || (name === "Keepframe Skew" && match === "ADBE Geometry2")
            || ((name === "Keepframe Gradient" || name === "Keepframe Fill") && match === "ADBE Ramp")
            || (name === "Keepframe Fill Matte" && match === "ADBE Set Matte3")
            || (/^Keepframe Shadow [1-9][0-9]*$/.test(name) && match === "ADBE Drop Shadow");
    }

    function managed(layer) {
        var t = transform(layer), result = [t.property("ADBE Anchor Point")], i, j, effect, child;
        var position = t.property("ADBE Position");
        if (position.dimensionsSeparated) {
            result.push(t.property("ADBE Position_0"), t.property("ADBE Position_1"));
            if (layer.threeDLayer) { result.push(t.property("ADBE Position_2")); }
        } else { result.push(position); }
        result.push(t.property("ADBE Scale"), t.property("ADBE Rotate Z"), t.property("ADBE Opacity"));
        if (layer.threeDLayer) {
            result.push(t.property("ADBE Rotate X"), t.property("ADBE Rotate Y"), t.property("ADBE Orientation"));
        }
        if (layer instanceof TextLayer) {
            result.push(layer.property("ADBE Text Properties").property("ADBE Text Document"));
        }
        var effects = layer.property("ADBE Effect Parade");
        for (i = 1; i <= effects.numProperties; i += 1) {
            effect = effects.property(i);
            if (owned(effect)) {
                for (j = 1; j <= effect.numProperties; j += 1) {
                    child = effect.property(j);
                    if (child.propertyType === PropertyType.PROPERTY
                        && child.propertyValueType !== PropertyValueType.NO_VALUE) { result.push(child); }
                }
            }
        }
        return result;
    }

    function rounded(value) {
        var result, i;
        if (typeof value === "number") { return Math.round(value * 10000) / 10000; }
        if (array(value)) {
            result = [];
            for (i = 0; i < value.length; i += 1) { result.push(rounded(value[i])); }
            return result;
        }
        return value;
    }

    function colorValue(value) {
        var result = [], i;
        for (i = 0; i < value.length; i += 1) { result.push(Math.round(value[i] * 255)); }
        return result;
    }

    function sameColor(a, b) {
        var i;
        for (i = 0; i < a.length; i += 1) { if (Math.abs(a[i] - b[i]) > 0.5 / 255) { return false; } }
        return true;
    }

    function propertyDimensions(p, temporal) {
        var type = p.propertyValueType;
        if (type === PropertyValueType.ThreeD || (!temporal && type === PropertyValueType.ThreeD_SPATIAL)) { return 3; }
        if (type === PropertyValueType.TwoD || (!temporal && type === PropertyValueType.TwoD_SPATIAL)) { return 2; }
        return 1;
    }

    function paddedValue(p, value, modelScale) {
        var dimensions = propertyDimensions(p, false), result;
        if (dimensions === 1 || !array(value)) { return value; }
        result = value.slice(0, dimensions);
        while (result.length < dimensions) {
            result.push(p.matchName === "ADBE Scale" ? (modelScale ? value[0] : 100) : 0);
        }
        return result;
    }

    function propertyValue(p, value, modelScale, layer, styled) {
        var result;
        if (p.propertyValueType === PropertyValueType.TEXT_DOCUMENT) {
            result = [value.text, value.font, rounded(value.fontSize), colorValue(value.fillColor), value.applyFill, String(value.justification)];
            // Styled text manages tracking and stroke; stroke fields throw while applyStroke is false.
            if (styled) {
                result.push(rounded(value.tracking), value.applyStroke, value.applyStroke
                    ? [colorValue(value.strokeColor), rounded(value.strokeWidth), value.strokeOverFill] : null);
            }
            return result;
        }
        // A layer parameter reads the referenced layer's index, which moves with the stack; "self" does not.
        if (p.propertyValueType === PropertyValueType.LAYER_INDEX) { return value === layer.index ? "self" : value; }
        return p.propertyValueType === PropertyValueType.COLOR ? colorValue(value) : rounded(paddedValue(p, value, modelScale));
    }

    function easeValues(p, eases) {
        var values = [], i, ease;
        for (i = 0; i < propertyDimensions(p, true); i += 1) {
            ease = eases[i < eases.length ? i : 0];
            values.push(rounded([ease.influence, ease.speed]));
        }
        return values;
    }

    function fingerprint(layer, fps, legacy, timings, styled) {
        var started = new Date().getTime(), value;
        try {
            var props = managed(layer), data = [], i, k, p, keys, key, incoming, outgoing, effects, effect, effectNames = [];
            var position = transform(layer).property("ADBE Position");
            var modelScale = kind(layer) === "model";
            // FPS is managed context: changing it changes how every frame-based property must be written.
            data.push(rounded(fps));
            // v1 read every ease; builds up to 1.0.238 also fingerprinted name and label.
            if (legacy === true) { data.push(layer.name, layer.label); }
            data.push(Math.round(layer.inPoint * fps), Math.round(layer.outPoint * fps), layer.threeDLayer, position.dimensionsSeparated,
                position.expressionEnabled, position.expression);
            if (kind(layer) === "solid") {
                data.push([layer.source.width, layer.source.height, colorValue(layer.source.mainSource.color)]);
            } else if (layer.source) { data.push(layer.source.id); }
            if (kind(layer) === "footage") { data.push(Math.round(layer.startTime * fps)); }
            // A time stretch other than 100 % (speed edits); unstretched layers keep their earlier fingerprint.
            if (kind(layer) === "footage" && Math.abs(layer.stretch - 100) > 1e-6) { data.push(rounded(layer.stretch)); }
            // Model bounds can change with new bytes even when fit_box and the layer spec stay identical.
            if (kind(layer) === "model") { data.push(layer.source.comment); }
            for (i = 0; i < props.length; i += 1) {
                p = props[i];
                keys = [];
                for (k = 1; k <= p.numKeys; k += 1) {
                    incoming = p.keyInInterpolationType(k); outgoing = p.keyOutInterpolationType(k);
                    key = [Math.round(p.keyTime(k) * fps), propertyValue(p, p.keyValue(k), modelScale, layer, styled), String(incoming), String(outgoing)];
                    // AE turns an ease-edited key Bezier; all-LINEAR ease has no visible state.
                    if (legacy || incoming !== KeyframeInterpolationType.LINEAR || outgoing !== KeyframeInterpolationType.LINEAR) {
                        key.push(easeValues(p, p.keyInTemporalEase(k)), easeValues(p, p.keyOutTemporalEase(k)));
                    }
                    keys.push(key);
                }
                data.push([p.matchName, p.numKeys ? keys : propertyValue(p, p.valueAtTime(0, true), modelScale, layer, styled),
                    p.canSetExpression ? p.expressionEnabled : false, p.canSetExpression ? p.expression : ""]);
            }
            effects = layer.property("ADBE Effect Parade");
            for (i = 1; i <= effects.numProperties; i += 1) {
                effect = effects.property(i);
                if (owned(effect)) { effectNames.push([effect.matchName, effect.name, effect.enabled]); }
            }
            data.push(effectNames);
        } finally {
            if (timings) { timings.read += new Date().getTime() - started; }
        }
        started = new Date().getTime();
        value = hash(JSON.stringify(data));
        if (timings) { timings.hash += new Date().getTime() - started; }
        return value;
    }

    function readFingerprint(layer, fps, legacy, timings, styled) {
        try { return fingerprint(layer, fps, legacy, timings, styled === true); } catch (e) { return null; }
    }

    function keyCount(layer) {
        var props = managed(layer), count = 0, i;
        for (i = 0; i < props.length; i += 1) { count += props[i].numKeys; }
        return count;
    }

    function clear(p) {
        var i;
        if (p.canSetExpression && p.expressionEnabled) { p.expressionEnabled = false; }
        for (i = p.numKeys; i >= 1; i -= 1) { p.removeKey(i); }
    }

    function staticValue(p, value) { clear(p); p.setValue(paddedValue(p, value, false)); }

    function eases(values, dimensions, factor) {
        var result = [], i, value;
        for (i = 0; i < dimensions; i += 1) {
            value = values ? values[i < values.length ? i : 0] : [33.33333333333333, 0];
            result.push(new KeyframeEase(value[1] * factor, value[0]));
        }
        return result;
    }

    function writeKeys(p, keys, fps, factor, modelScale) {
        var i, k, value, times = [], values = [], dimensions = propertyDimensions(p, true), linearize;
        clear(p);
        for (i = 0; i < keys.length; i += 1) {
            k = keys[i];
            value = k[1];
            if (modelScale) { value = [value[0] * factor, value[1] * factor]; }
            times.push(k[0] / fps); values.push(paddedValue(p, value, modelScale));
        }
        if (keys.length === 1) { p.setValue(values[0]); return; }
        // One call for all keys; per-key calls only where an ease is set (linear keys keep AE's defaults).
        p.setValuesAtTimes(times, values);
        linearize = p.keyInInterpolationType(1) !== KeyframeInterpolationType.LINEAR;
        for (i = 0; i < keys.length; i += 1) {
            k = keys[i];
            if (k[2] || k[3]) {
                p.setTemporalEaseAtKey(i + 1, eases(k[3], dimensions, factor), eases(k[2], dimensions, factor));
                p.setInterpolationTypeAtKey(i + 1, k[3] ? KeyframeInterpolationType.BEZIER : KeyframeInterpolationType.LINEAR,
                    k[2] ? KeyframeInterpolationType.BEZIER : KeyframeInterpolationType.LINEAR);
            } else if (linearize) {
                p.setInterpolationTypeAtKey(i + 1, KeyframeInterpolationType.LINEAR, KeyframeInterpolationType.LINEAR);
            }
        }
    }

    function addEffect(layer, name, match, force) {
        var effects = layer.property("ADBE Effect Parade"), effect, i, j, child, defaults;
        for (i = 1; i <= effects.numProperties; i += 1) {
            effect = effects.property(i);
            if (effect.name === name && effect.matchName === match) {
                if (force) {
                    // Read this AE build's defaults without replacing the owned effect.
                    defaults = {};
                    effect = effects.addProperty(match);
                    try {
                        for (j = 1; j <= effect.numProperties; j += 1) {
                            child = effect.property(j);
                            if (child.propertyType === PropertyType.PROPERTY
                                && child.propertyValueType !== PropertyValueType.NO_VALUE) {
                                defaults["$" + child.matchName] = child.valueAtTime(0, true);
                            }
                        }
                    } finally { effect.remove(); }
                    // Adding/removing an indexed property invalidates earlier AE references.
                    effect = effects.property(i);
                }
                for (j = 1; j <= effect.numProperties; j += 1) {
                    child = effect.property(j);
                    if (child.propertyType === PropertyType.PROPERTY
                        && child.propertyValueType !== PropertyValueType.NO_VALUE) {
                        clear(child);
                        if (force) { child.setValue(defaults["$" + child.matchName]); }
                    }
                }
                return effect;
            }
        }
        effect = effects.addProperty(match);
        effect.name = name;
        return effect;
    }

    function shifted(keys, origin) {
        var result = [], i;
        for (i = 0; i < keys.length; i += 1) {
            result.push([keys[i][0], [keys[i][1][0] + origin[0], keys[i][1][1] + origin[1]], keys[i][2], keys[i][3]]);
        }
        return result;
    }

    function writeRamp(effect, ramp, fps, origin) {
        writeKeys(effect.property("ADBE Ramp-0001"), shifted(ramp.start, origin), fps, 1, false);
        writeKeys(effect.property("ADBE Ramp-0002"), ramp.start_color, fps, 1, false);
        writeKeys(effect.property("ADBE Ramp-0003"), shifted(ramp.end, origin), fps, 1, false);
        writeKeys(effect.property("ADBE Ramp-0004"), ramp.end_color, fps, 1, false);
        staticValue(effect.property("ADBE Ramp-0005"), ramp.shape);
    }

    function ownedIndex(effects, name) {
        var i;
        for (i = 1; i <= effects.numProperties; i += 1) {
            if (effects.property(i).name === name && owned(effects.property(i))) { return i; }
        }
        return 0;
    }

    function writeEffects(layer, s, fps, anchor, force, origin) {
        var effects = layer.property("ADBE Effect Parade"), i, effect, match, shift;
        var gradient = optional(s.effects.gradient), fill = optional(s.effects.fill), shadows = optional(s.effects.shadows) || [];
        for (i = effects.numProperties; i >= 1; i -= 1) {
            effect = effects.property(i);
            match = /^Keepframe Shadow ([0-9]+)$/.exec(effect.name);
            if (owned(effect) && ((effect.name === "Keepframe Reveal" && s.effects.reveal === null)
                || (effect.name === "Keepframe Skew" && s.effects.skew === null)
                || (effect.name === "Keepframe Gradient" && gradient === null)
                || ((effect.name === "Keepframe Fill" || effect.name === "Keepframe Fill Matte") && fill === null)
                || (match && Number(match[1]) > shadows.length))) { effect.remove(); }
        }
        if (fill !== null) {
            writeRamp(addEffect(layer, "Keepframe Fill", "ADBE Ramp", force), fill, fps, origin);
            effect = addEffect(layer, "Keepframe Fill Matte", "ADBE Set Matte3", force);
            // The matte is the text's own alpha (Set Matte reads the layer's source, before any effect).
            staticValue(effect.property("ADBE Set Matte3-0001"), layer.index);
            staticValue(effect.property("ADBE Set Matte3-0002"), 4);
        }
        if (gradient !== null) { writeRamp(addEffect(layer, "Keepframe Gradient", "ADBE Ramp", force), gradient, fps, [0, 0]); }
        if (s.effects.reveal !== null) {
            effect = addEffect(layer, "Keepframe Reveal", "ADBE Linear Wipe", force);
            writeKeys(effect.property("ADBE Linear Wipe-0001"), s.effects.reveal.completion, fps, 1, false);
            staticValue(effect.property("ADBE Linear Wipe-0002"), s.effects.reveal.angle);
            staticValue(effect.property("ADBE Linear Wipe-0003"), s.effects.reveal.feather);
        }
        if (s.effects.skew !== null) {
            effect = addEffect(layer, "Keepframe Skew", "ADBE Geometry2", force);
            writeKeys(effect.property("ADBE Geometry2-0005"), s.effects.skew.skew, fps, 1, false);
            staticValue(effect.property("ADBE Geometry2-0006"), s.effects.skew.axis);
            staticValue(effect.property("ADBE Geometry2-0001"), [anchor[0], anchor[1]]);
            // Text shear pivots on the first baseline (y = 0), the element skew on the anchor.
            shift = s.effects.skew.baseline_shear === undefined ? 0 : s.effects.skew.baseline_shear * anchor[1];
            staticValue(effect.property("ADBE Geometry2-0002"), [anchor[0] + shift, anchor[1]]);
        }
        for (i = 0; i < shadows.length; i += 1) {
            effect = addEffect(layer, "Keepframe Shadow " + (i + 1), "ADBE Drop Shadow", force);
            staticValue(effect.property("ADBE Drop Shadow-0001"), shadows[i].color);
            // Scripts see Drop Shadow opacity as 0-255 (the panel shows a percentage).
            staticValue(effect.property("ADBE Drop Shadow-0002"), shadows[i].opacity * 255 / 100);
            staticValue(effect.property("ADBE Drop Shadow-0003"), shadows[i].direction);
            staticValue(effect.property("ADBE Drop Shadow-0004"), shadows[i].distance);
            staticValue(effect.property("ADBE Drop Shadow-0005"), shadows[i].softness);
        }
        if (fill !== null) {
            // The fill replaces the text's pixels, so it runs before every other effect (moving invalidates references).
            if (ownedIndex(effects, "Keepframe Fill") !== 1) { effects.property(ownedIndex(effects, "Keepframe Fill")).moveTo(1); }
            if (ownedIndex(effects, "Keepframe Fill Matte") !== 2) { effects.property(ownedIndex(effects, "Keepframe Fill Matte")).moveTo(2); }
        }
    }

    function writeLayer(layer, s, fps, force, timings, wasStyled) {
        var t, rect, anchor = s.anchor, doc, i, factor = 1, origin = [0, 0], start, stroke, end, stretch = 100;
        if (layer.threeDLayer !== (s.kind === "model")) { layer.threeDLayer = s.kind === "model"; }
        t = transform(layer);
        if (!t.property("ADBE Position").dimensionsSeparated) { t.property("ADBE Position").dimensionsSeparated = true; }
        // The leader can also carry an expression after separation.
        if (t.property("ADBE Position").expressionEnabled) { t.property("ADBE Position").expressionEnabled = false; }
        if (layer.threeDLayer) {
            staticValue(t.property("ADBE Position_2"), 0);
            staticValue(t.property("ADBE Orientation"), [0, 0, 0]);
        }
        if (s.kind === "text") {
            doc = layer.property("ADBE Text Properties").property("ADBE Text Document").valueAtTime(0, true);
            doc.text = s.source.text;
            doc.font = s.source.font.postscript !== null ? s.source.font.postscript : s.source.font.family;
            doc.fontSize = s.source.size_px;
            doc.applyFill = true;
            doc.fillColor = rgb(s.source.color);
            doc.justification = ParagraphJustification.LEFT_JUSTIFY;
            if (styledText(s)) {
                doc.tracking = s.source.tracking;
                stroke = s.source.stroke;
                doc.applyStroke = stroke !== null;
                if (stroke !== null) {
                    doc.strokeColor = rgb(stroke.color);
                    doc.strokeWidth = stroke.width;
                    doc.strokeOverFill = stroke.over_fill;
                }
            } else if (wasStyled === true) {
                // The style was removed: clear what Keepframe set (unstyled text leaves these to the user).
                doc.tracking = 0;
                doc.applyStroke = false;
            }
            staticValue(layer.property("ADBE Text Properties").property("ADBE Text Document"), doc);
            rect = layer.sourceRectAtTime(s["in"] / fps, false);
            anchor = [rect.left + s.source.anchor_fraction[0] * s.source.box[0],
                rect.top + rect.height / 2 + (s.source.anchor_fraction[1] - 0.5) * s.source.box[1]];
            origin = [anchor[0] - s.source.anchor_fraction[0] * s.source.box[0],
                anchor[1] - s.source.anchor_fraction[1] * s.source.box[1]];
        }
        if (layer.threeDLayer) {
            rect = layer.sourceRectAtTime(0, false);
            requireValue(rect.width > 0 && rect.height > 0, "model has an empty source rectangle");
            anchor = [rect.left + rect.width / 2, rect.top + rect.height / 2, 0];
            factor = 0.9 * Math.min(s.source.fit_box[0] / rect.width, s.source.fit_box[1] / rect.height);
            writeKeys(t.property("ADBE Rotate X"), s.props.rotation_x, fps, 1, false);
            writeKeys(t.property("ADBE Rotate Y"), s.props.rotation_y, fps, 1, false);
        }
        staticValue(t.property("ADBE Anchor Point"), anchor);
        for (i = 0; i < PROP_NAMES.length; i += 1) {
            writeKeys(t.property(PROP_MATCHES[i]), s.props[PROP_NAMES[i]], fps,
                i === 2 ? factor : 1, i === 2 && s.kind === "model");
        }
        writeEffects(layer, s, fps, anchor, force, origin);
        if (s.kind === "footage") {
            // A speed edit plays the clip at its rate: time stretch 100 / rate (AE scales in/out around startTime).
            if (s.source.stretch !== undefined) { stretch = s.source.stretch; }
            if (Math.abs(layer.stretch - stretch) > 1e-6) { layer.stretch = stretch; }
            // On the frame grid; the clip's first frame shows at the layer's first visible frame. Before in/out,
            // which a clip clamps to [startTime, startTime + its stretched duration].
            start = Math.round(s.source.start_time * fps) / fps;
            if (layer.startTime !== start) { layer.startTime = start; }
        }
        end = (s.out + 1) / fps;
        // A clip ends with its footage (no time remapping); never ask AE for more.
        if (s.kind === "footage" && layer.source.duration > 0) {
            end = Math.min(end, start + layer.source.duration * stretch / 100);
        }
        if (s["in"] / fps >= layer.outPoint) { layer.outPoint = end; }
        layer.inPoint = s["in"] / fps;
        layer.outPoint = end;
        layer.comment = "keepframe:" + s.id + ";spec=" + layerHash(s, timings) + ";fp=" + (readFingerprint(layer, fps, false, timings, styledText(s)) || "00000000");
    }

    function folder(name, parent) {
        var items = app.project.items, i, item;
        for (i = 1; i <= items.length; i += 1) {
            item = items[i];
            if (item instanceof FolderItem && item.name === name
                && item.parentFolder.id === (parent || app.project.rootFolder).id) { return item; }
        }
        item = items.addFolder(name);
        if (parent) { item.parentFolder = parent; }
        return item;
    }

    function findComp(tag) {
        var items = app.project.items, i;
        for (i = 1; i <= items.length; i += 1) {
            if (items[i] instanceof CompItem && items[i].comment === tag) { return items[i]; }
        }
        return null;
    }

    function syncAssets(spec, paths, parent) {
        var result = {}, i, j, asset, item, found, named;
        for (i = 0; i < spec.assets.length; i += 1) {
            asset = spec.assets[i]; found = null; named = null;
            for (j = 1; j <= parent.items.length; j += 1) {
                item = parent.items[j];
                if (!(item instanceof FootageItem)) { continue; }
                if (item.comment === "keepframe-asset:" + asset.sha256) { found = item; break; }
                if (item.name === asset.name && /^keepframe-asset:[0-9a-f]{64}$/.test(item.comment)) { named = item; }
            }
            if (!found) {
                if (named) { found = named; found.replace(new File(paths[asset.name])); }
                else {
                    found = app.project.importFile(new ImportOptions(new File(paths[asset.name])));
                    found.name = asset.name;
                    found.parentFolder = parent;
                }
                // Sprite clips (ProRes 4444) carry straight alpha; AE would otherwise guess or ask.
                if (/\.mov$/i.test(asset.name)) { found.mainSource.alphaMode = AlphaMode.STRAIGHT; }
                found.comment = "keepframe-asset:" + asset.sha256;
            }
            result["$" + asset.name] = found;
        }
        return result;
    }

    function createLayer(comp, s, assets) {
        var layer;
        if (s.kind === "text") { layer = comp.layers.addText(s.source.text); }
        else if (s.kind === "solid" || s.kind === "null") {
            layer = s.kind === "solid" ? comp.layers.addSolid(rgb(s.source.color), s.name, comp.width, comp.height, 1, comp.duration)
                : comp.layers.addNull(comp.duration);
        } else { layer = comp.layers.add(assets["$" + s.source.asset]); }
        // Claim immediately: errors after creation must leave a recoverable tagged layer.
        layer.comment = "keepframe:" + s.id;
        layer.label = s.label === null ? 0 : s.label;
        layer.name = s.name;
        // Visibility belongs to the user after creation, including the glyph-image layer.
        if (s.hidden === true) { layer.enabled = false; }
        if (s.kind === "solid" || s.kind === "null") { layer.source.parentFolder = comp.parentFolder; }
        return layer;
    }

    function updateSource(layer, comp, s, assets) {
        var temporary;
        if ((s.kind === "image" || s.kind === "model" || s.kind === "footage") && layer.source.id !== assets["$" + s.source.asset].id) {
            layer.replaceSource(assets["$" + s.source.asset], false);
            return true;
        }
        if (s.kind === "solid" && (layer.source.width !== comp.width || layer.source.height !== comp.height
            || !sameColor(layer.source.mainSource.color, rgb(s.source.color)))) {
            // A new solid source avoids changing another layer that shares the old, untagged footage.
            temporary = createLayer(comp, s, assets);
            try { layer.replaceSource(temporary.source, false); } finally { temporary.remove(); }
            return true;
        }
        return false;
    }

    function tag(layer) {
        var match = /^keepframe:([\s\S]*);spec=([0-9a-f]{8});fp=([0-9a-f]{8})$/.exec(layer.comment);
        if (match) { return {id: match[1], spec: match[2], fp: match[3], layer: layer}; }
        match = /^keepframe:([\s\S]+?)(?:;spec=([0-9a-f]{8}))?$/.exec(layer.comment);
        return match ? {id: match[1], spec: match[2] || null, fp: null, layer: layer} : null;
    }

    function tagged(comp) {
        var result = [], i, record;
        if (comp) {
            for (i = 1; i <= comp.layers.length; i += 1) {
                record = tag(comp.layers[i]);
                if (record) { result.push(record); }
            }
        }
        return result;
    }

    function kind(layer) {
        if (layer instanceof TextLayer) { return "text"; }
        if (layer.nullLayer) { return "null"; }
        if (layer.source instanceof FootageItem) {
            if (layer.source.file) {
                if (/\.glb$/i.test(layer.source.file.name)) { return "model"; }
                return /\.(mp4|mov)$/i.test(layer.source.file.name) ? "footage" : "image";
            }
            return "solid";
        }
        return "unknown";
    }

    function hasExpression(layer) {
        var props = managed(layer), i;
        if (transform(layer).property("ADBE Position").expressionEnabled) { return true; }
        for (i = 0; i < props.length; i += 1) {
            if (props[i].canSetExpression && props[i].expressionEnabled) { return true; }
        }
        return false;
    }

    function hasExtras(layer) {
        var effects = layer.property("ADBE Effect Parade"), i;
        if (layer.parent || layer.trackMatteLayer
            || layer.property("ADBE Mask Parade").numProperties > 0 || hasExpression(layer)) { return true; }
        for (i = 1; i <= effects.numProperties; i += 1) {
            if (!owned(effects.property(i))) { return true; }
        }
        return false;
    }

    function hasUserDependents(comp, target) {
        var i, layer;
        for (i = 1; i <= comp.layers.length; i += 1) {
            layer = comp.layers[i];
            if ((layer.parent && layer.parent.index === target.index)
                || (layer.trackMatteLayer && layer.trackMatteLayer.index === target.index)) { return true; }
        }
        return false;
    }

    function modelRenderer(comp) {
        var available, i, version;
        if (!comp) {
            // AE exposes renderers only on comps. Advanced 3D ships from AE 24.0;
            // an empty project is version-checked before creating its first comp.
            for (i = 1; i <= app.project.items.length; i += 1) {
                if (app.project.items[i] instanceof CompItem) { comp = app.project.items[i]; break; }
            }
            if (!comp) {
                version = parseFloat(app.version);
                if (version >= 24.0) { return "ADBE Calder"; }
                throw new Error("this After Effects has no Advanced 3D renderer (needed for 3D models)");
            }
        }
        available = comp.renderers;
        for (i = 0; i < available.length; i += 1) {
            if (available[i] === "ADBE Calder") { return available[i]; }
        }
        throw new Error("this After Effects has no Advanced 3D renderer (needed for 3D models)");
    }

    function updateComp(comp, s) {
        var names = ["name", "width", "height", "pixelAspect", "frameRate", "duration"], i;
        var values = [s.name, s.width, s.height, 1, s.fps, s.frames / s.fps];
        for (i = 0; i < names.length; i += 1) {
            if (names[i] === "frameRate") {
                if (Math.abs(comp.frameRate - s.fps) > 0.0001) { comp.frameRate = s.fps; }
            } else if (names[i] === "duration") {
                if (Math.round(comp.duration * s.fps) !== s.frames) { comp.duration = values[i]; }
            } else if (comp[names[i]] !== values[i]) { comp[names[i]] = values[i]; }
        }
    }

    function placeNew(layer, s, ordered, existing) {
        var i, j, neighbour;
        if (s.kind === "text" && s.hidden === true && /~text$/.test(s.id)) {
            neighbour = existing["$" + s.id.slice(0, -5)];
            if (neighbour) {
                if (layer.index + 1 !== neighbour.layer.index) { layer.moveBefore(neighbour.layer); }
                return;
            }
        }
        for (i = 0; i < ordered.length; i += 1) { if (ordered[i].id === s.id) { break; } }
        for (j = i - 1; j >= 0; j -= 1) {
            neighbour = existing["$" + ordered[j].id];
            if (neighbour) {
                if (layer.index !== neighbour.layer.index + 1) { layer.moveAfter(neighbour.layer); }
                return;
            }
        }
        for (j = i + 1; j < ordered.length; j += 1) {
            neighbour = existing["$" + ordered[j].id];
            if (neighbour) {
                if (layer.index + 1 !== neighbour.layer.index) { layer.moveBefore(neighbour.layer); }
                return;
            }
        }
    }

    function orderLayers(comp, desired) {
        var current = tagged(comp), rank = {}, length = [], previous = [], keep = {}, i, j, best = -1, anchor = -1;
        desired.sort(function (a, b) { return b.order - a.order || a.sequence - b.sequence; });
        for (i = 0; i < desired.length; i += 1) { rank["$" + desired[i].id] = i; }
        // ponytail: O(n squared) LIS; use binary-search tails if large scenes make ordering slow.
        for (i = 0; i < current.length; i += 1) {
            length[i] = 1; previous[i] = -1;
            for (j = 0; j < i; j += 1) {
                if (rank["$" + current[j].id] < rank["$" + current[i].id] && length[j] + 1 > length[i]) {
                    length[i] = length[j] + 1; previous[i] = j;
                }
            }
            if (best === -1 || length[i] > length[best]) { best = i; }
        }
        while (best !== -1) {
            keep["$" + current[best].id] = true;
            anchor = Math.max(anchor, rank["$" + current[best].id]);
            best = previous[best];
        }
        // Work outward from the lowest kept anchor: each neighbour is already placed.
        for (i = anchor - 1; i >= 0; i -= 1) {
            if (own(keep, "$" + desired[i].id)) { continue; }
            desired[i].layer.moveBefore(desired[i + 1].layer);
        }
        for (i = anchor + 1; i < desired.length; i += 1) {
            desired[i].layer.moveAfter(desired[i - 1].layer);
        }
    }

    function errorResult(e) {
        return JSON.stringify({ok: false, error: String(e.message || e), line: e.line || 0});
    }

    global.kfInfo = function (withFonts) {
        try {
            var fonts = [], all, i, j, font;
            var info = {ok: true, ae_version: app.version, project_name: app.project.file ? decodeURI(app.project.file.name) : null,
                project_saved: app.project.file !== null, host_build: HOST_BUILD};
            if (withFonts !== "false") {
                all = app.fonts.allFonts;
                for (i = 0; i < all.length; i += 1) {
                    for (j = 0; j < all[i].length; j += 1) {
                        font = all[i][j];
                        fonts.push({family: font.familyName, style: font.styleName, postscript: font.postScriptName});
                    }
                }
                info.fonts = fonts;
            }
            return JSON.stringify(info);
        } catch (e) { return errorResult(e); }
    };

    global.kfRender = function (requestJson) {
        var folder;
        try {
            var request = JSON.parse(requestJson), comp, frames, seen = {}, i, f, name, file, stale;
            var result = [];
            requireValue(request && typeof request.job === "string" && /^j_[0-9a-f]{16}$/.test(request.job), "Invalid render job");
            requireValue(typeof request.tag === "string" && /^keepframe:[^\/\x00-\x1f]+\/[^\/\x00-\x1f]+$/.test(request.tag), "Invalid render tag");
            frames = request.frames;
            requireValue(array(frames) && frames.length > 0 && frames.length <= 16, "Invalid render frames");
            comp = findComp(request.tag);
            requireValue(comp, "the Keepframe comp is missing; send the scene to AE first");
            for (i = 0; i < frames.length; i += 1) {
                f = frames[i];
                requireValue(number(f) && Math.floor(f) === f && f >= 0 &&
                    f < Math.round(comp.duration * comp.frameRate) && !own(seen, "$" + f), "Invalid render frames");
                seen["$" + f] = true;
            }
            requireValue(typeof comp.saveFrameToPng === "function",
                "this After Effects cannot export frames; update to After Effects 24.1 or newer");
            folder = new Folder(Folder.temp.fsName + "/keepframe-" + request.job);
            requireValue(folder.exists || folder.create(), "Could not create temporary frame folder");
            stale = folder.getFiles();
            for (i = 0; i < stale.length; i += 1) {
                requireValue(stale[i] instanceof File && stale[i].remove(), "Could not remove stale frame file");
            }
            for (i = 0; i < frames.length; i += 1) {
                f = frames[i]; name = String(f);
                while (name.length < 4) { name = "0" + name; }
                file = new File(folder.fsName + "/frame_" + name + ".png");
                comp.saveFrameToPng(f / comp.frameRate, file);
                result.push({frame: f, path: file.fsName});
            }
            return JSON.stringify({ok: true, frames: result, width: comp.width, height: comp.height});
        } catch (e) {
            if (folder) {
                return errorResult({message: String(e.message || e).split(folder.fsName).join("[temporary frames]"), line: e.line || 0});
            }
            return errorResult(e);
        }
    };

    global.kfSync = function (specJson, assetsJson, force) {
        try {
            var started = new Date().getTime(), stage = started, measured;
            var timings = {validate: 0, read: 0, hash: 0, assets: 0, write: 0, order: 0, total: 0};
            var spec = JSON.parse(specJson), paths = JSON.parse(assetsJson), parent, comp, assets, i, layer, record;
            var existing = {}, wanted = {}, records, s, newHash, edits = [], interrupted = [], desired = [], renderer = null, ordered, fps, other;
            validate(spec, paths, force);
            timings.validate = new Date().getTime() - stage;
            var result = {ok: true, applied: true, created: [], updated: [], deleted: [], unchanged: 0,
                keys: {}, warnings: spec.warnings.slice(0), ae_version: app.version, timings: timings};
            comp = findComp(spec.comp.tag);
            records = tagged(comp);
            for (i = 0; i < spec.layers.length; i += 1) { wanted["$" + spec.layers[i].id] = spec.layers[i]; }
            for (i = 0; i < records.length; i += 1) {
                record = records[i];
                requireValue(!own(existing, "$" + record.id), "two layers are tagged " + record.id
                    + " (a duplicated Keepframe layer). Delete the copy, or keep it by clearing its layer comment, then send again");
                existing["$" + record.id] = record;
            }
            for (i = 0; i < spec.layers.length; i += 1) {
                if (spec.layers[i].kind === "model") { renderer = modelRenderer(comp); break; }
            }
            for (i = 0; i < records.length; i += 1) {
                record = records[i]; s = wanted["$" + record.id];
                fps = Math.abs(comp.frameRate - spec.comp.fps) <= 0.0001 ? spec.comp.fps : rounded(comp.frameRate);
                record.fps = fps;
                record.kind = kind(record.layer);
                record.styled = styledText(s);
                record.current = readFingerprint(record.layer, fps, false, timings, record.styled);
                if (record.kind === "text" && record.fp !== null && record.current !== record.fp) {
                    // Synced before its text was styled (or with a style since removed): the other variant.
                    other = readFingerprint(record.layer, fps, false, timings, !record.styled);
                    if (other !== null && other === record.fp) { record.current = other; record.styled = !record.styled; }
                }
                record.asset = record.kind === "model" ? record.layer.source.comment : null;
                record.upgrade = record.fp !== null && record.current !== null && record.current !== record.fp
                    && (readFingerprint(record.layer, fps, "ease", timings) === record.fp
                        || readFingerprint(record.layer, fps, true, timings) === record.fp);
                if (record.upgrade) { record.fp = record.current; }
                if (force !== "true") {
                    if (record.fp === null) { interrupted.push(record.id); }
                    try {
                        if (record.current === null || record.current !== record.fp
                            || hasExpression(record.layer)
                            || ((!s || s.kind !== record.kind) && (hasExtras(record.layer)
                                || hasUserDependents(comp, record.layer)))) { edits.push(record.id); }
                    } catch (readError) { edits.push(record.id); }
                }
            }
            if (edits.length) {
                result = {ok: true, applied: false, hand_edited: edits, timings: timings};
                if (interrupted.length) { result.interrupted = interrupted; }
                timings.total = new Date().getTime() - started;
                return JSON.stringify(result);
            }
            app.beginUndoGroup("Keepframe sync");
            try {
                stage = new Date().getTime();
                parent = folder(spec.project, folder("Keepframe", null));
                if (!comp) {
                    comp = app.project.items.addComp(spec.comp.name, spec.comp.width, spec.comp.height, 1,
                        spec.comp.frames / spec.comp.fps, spec.comp.fps);
                    comp.comment = spec.comp.tag;
                    comp.parentFolder = parent;
                }
                updateComp(comp, spec.comp);
                if (renderer) {
                    renderer = modelRenderer(comp);
                    if (comp.renderer !== renderer) { comp.renderer = renderer; }
                }
                timings.write += new Date().getTime() - stage;
                stage = new Date().getTime();
                assets = syncAssets(spec, paths, parent);
                timings.assets += new Date().getTime() - stage;
                stage = new Date().getTime();
                for (i = 0; i < records.length; i += 1) {
                    record = records[i]; s = wanted["$" + record.id];
                    if (!s) {
                        record.layer.remove(); result.deleted.push(record.id);
                        delete existing["$" + record.id];
                    }
                }
                ordered = spec.layers.slice(0);
                ordered.sort(function (a, b) { return b.order - a.order; });
                // Settle existing order first, so each insertion needs only its neighbour move.
                for (i = 0; i < spec.layers.length; i += 1) {
                    s = spec.layers[i]; record = existing["$" + s.id];
                    if (record) { desired.push({id: s.id, layer: record.layer, order: s.order, sequence: i}); }
                }
                timings.write += new Date().getTime() - stage;
                stage = new Date().getTime();
                orderLayers(comp, desired);
                timings.order += new Date().getTime() - stage;
                desired = [];
                stage = new Date().getTime();
                measured = timings.read + timings.hash;
                for (i = 0; i < spec.layers.length; i += 1) {
                    s = spec.layers[i]; record = existing["$" + s.id];
                    newHash = layerHash(s, timings);
                    if (record && record.spec === newHash && record.current !== null && record.fp === record.current
                        && record.fps === spec.comp.fps && (record.kind !== "model" || record.asset === record.layer.source.comment)
                        && (s.kind !== "solid" || (record.layer.source.width === comp.width && record.layer.source.height === comp.height))) {
                        layer = record.layer; result.unchanged += 1;
                        if (record.upgrade) {
                            layer.comment = "keepframe:" + s.id + ";spec=" + record.spec + ";fp=" + record.current;
                        }
                    } else {
                        if (record && record.kind !== s.kind) {
                            layer = createLayer(comp, s, assets);
                            // Move against the old tagged layer before removing it: user neighbours retain their places.
                            if (layer.index + 1 !== record.layer.index) { layer.moveBefore(record.layer); }
                            record.layer.remove(); result.deleted.push(s.id);
                            record = null;
                        } else {
                            layer = record ? record.layer : createLayer(comp, s, assets);
                            if (record) { updateSource(layer, comp, s, assets); }
                            else { placeNew(layer, s, ordered, existing); }
                        }
                        writeLayer(layer, s, spec.comp.fps, force === "true", timings, record ? record.styled : false);
                        (record ? result.updated : result.created).push(s.id);
                    }
                    result.keys[s.id] = keyCount(layer);
                    result.warnings = result.warnings.concat(s.warnings);
                    existing["$" + s.id] = {layer: layer};
                    desired.push({id: s.id, layer: layer, order: s.order, sequence: i});
                }
                timings.write += new Date().getTime() - stage - (timings.read + timings.hash - measured);
                stage = new Date().getTime();
                orderLayers(comp, desired);
                timings.order += new Date().getTime() - stage;
            } finally { app.endUndoGroup(); }
            timings.total = new Date().getTime() - started;
            return JSON.stringify(result);
        } catch (e) { return errorResult(e); }
    };
}(this));
