/* Keepframe After Effects panel: AE 2022+ / ExtendScript.
 *
 * The bridge vocabulary is deliberately closed.  Commands and operations are
 * selected only by the switches below; command data is never executable text.
 */
(function (global) {
    var BRIDGE_SCHEMA_VERSION = 1;
    var LOCAL_APPDATA = $.getenv("LOCALAPPDATA");
    var BRIDGE_ROOT = Folder((LOCAL_APPDATA || Folder.userData.fsName) + "/Keepframe/ae-bridge");
    var COMMAND_FILE = File(BRIDGE_ROOT.fsName + "/command.json");
    var RESULT_FILE = File(BRIDGE_ROOT.fsName + "/result.json");
    var COMPLETED_DIR = Folder(BRIDGE_ROOT.fsName + "/completed");
    var INFLIGHT_DIR = Folder(BRIDGE_ROOT.fsName + "/inflight");
    var STOP_FILE = File(BRIDGE_ROOT.fsName + "/stop.json");
    var ASSET_DIR = Folder(BRIDGE_ROOT.fsName + "/assets");
    var CHECKPOINT_DIR = Folder(BRIDGE_ROOT.fsName + "/checkpoints");
    var RENDER_DIR = Folder(BRIDGE_ROOT.fsName + "/renders");
    var MAX_JSON_BYTES = 1048576;
    var MAX_KEYFRAMES = 1000000;
    var SESSION_COMP_MARKER_PREFIX = "keepframe:session:v1:";
    var SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
    var SESSION_COMP = null;
    var SESSION_MARKER = null;
    var panelWindow = null;
    var pollScheduled = false;
    var activeBatch = false;

    function ensureFolders() {
        if (!BRIDGE_ROOT.exists) { BRIDGE_ROOT.create(); }
        if (!COMPLETED_DIR.exists) { COMPLETED_DIR.create(); }
        if (!INFLIGHT_DIR.exists) { INFLIGHT_DIR.create(); }
        if (!ASSET_DIR.exists) { ASSET_DIR.create(); }
        if (!CHECKPOINT_DIR.exists) { CHECKPOINT_DIR.create(); }
        if (!RENDER_DIR.exists) { RENDER_DIR.create(); }
    }

    function readText(file) {
        if (!file.exists) { return null; }
        if (!file.open("r")) { return null; }
        file.encoding = "UTF-8";
        var text = file.read();
        file.close();
        if (text.length > MAX_JSON_BYTES) { return null; }
        return text;
    }

    function readJson(file) {
        var text = readText(file);
        if (text === null || text === "") { return null; }
        try {
            return JSON.parse(text);
        } catch (ignored) {
            return null;
        }
    }

    function writeAtomic(file, object) {
        var text = JSON.stringify(object);
        if (text.length > MAX_JSON_BYTES) { return false; }
        var temp = File(file.fsName + ".tmp");
        if (temp.exists) { temp.remove(); }
        if (!temp.open("w")) { return false; }
        temp.encoding = "UTF-8";
        temp.write(text);
        temp.close();
        if (file.exists) { file.remove(); }
        return temp.rename(file.name);
    }

    function writeImmutable(file, object) {
        var text = JSON.stringify(object);
        if (text.length > MAX_JSON_BYTES) { return false; }
        if (file.exists) {
            var existing = readText(file);
            return existing === text;
        }
        var temp = File(file.fsName + "." + String(new Date().getTime()) + ".tmp");
        if (!temp.open("w")) { return false; }
        temp.encoding = "UTF-8";
        temp.write(text);
        temp.close();
        if (file.exists) {
            temp.remove();
            return readText(file) === text;
        }
        var renamed = temp.rename(file.name);
        if (!renamed && temp.exists) { temp.remove(); }
        return renamed;
    }

    function removeStop() {
        if (STOP_FILE.exists && !activeBatch) { STOP_FILE.remove(); }
    }

    function versionInfo() {
        var major = parseInt(String(app.version).split(".")[0], 10);
        return {
            version: String(app.version),
            major: isFinite(major) ? major : 0,
            host: "after-effects"
        };
    }

    var FIXED_EFFECT_TRANSLATIONS = {
        "ADBE Gaussian Blur 2": {
            properties: { "ADBE Gaussian Blur 2-0001": "number" }
        },
        "ADBE Fill": {
            properties: { "ADBE Fill-0002": "color" }
        }
    };

    function fixedPropertySchemas() {
        return {
            "ADBE Position": "vec2",
            "ADBE Scale": "vec2",
            "ADBE Scale X": "number",
            "ADBE Scale Y": "number",
            "ADBE Rotate Z": "number",
            "ADBE Skew": "number",
            "ADBE Skew Axis": "number",
            "ADBE Opacity": "number",
        };
    }

    function fontLocalPath(font) {
        var location = null;
        try {
            if (font.location !== undefined && font.location !== null && String(font.location) !== "") {
                location = font.location;
            } else if (font.file !== undefined && font.file !== null && String(font.file) !== "") {
                location = font.file;
            }
            if (location && location.fsName !== undefined) {
                location = location.fsName;
            } else if (location && location.fullName !== undefined) {
                location = location.fullName;
            }
            if (location === null || location === undefined || String(location) === "") {
                return null;
            }
            return String(location);
        } catch (ignored) {
            return null;
        }
    }

    function fontMetadata(font, includeLocalPath) {
        var matchName = "";
        var family = "";
        var style = "";
        var version = null;
        var localPath;
        var metadata;
        try {
            matchName = String(font.postScriptName || font.fontName || font.fullName || "");
            family = String(font.familyName || font.family || "");
            style = String(font.styleName || font.style || "");
            if (font.version !== undefined && font.version !== null && String(font.version) !== "") {
                version = String(font.version);
            } else if (font.versionString !== undefined && font.versionString !== null && String(font.versionString) !== "") {
                version = String(font.versionString);
            }
            if (includeLocalPath === true) { localPath = fontLocalPath(font); }
        } catch (ignored) {
            return null;
        }
        if (!matchName) { return null; }
        metadata = {
            match_name: matchName,
            family: family,
            style: style,
            version: version,
            version_or_hash: version
        };
        if (includeLocalPath === true && localPath) { metadata.local_path = localPath; }
        return metadata;
    }

    function enumerateFonts(includeLocalPath) {
        var result = { font_names: [], fonts: [] };
        var fontApi;
        var allFonts;
        var group;
        var font;
        var metadata;
        var index;
        var inner;
        var innerIndex;
        try {
            if (!app || typeof app.fonts === "undefined" || !app.fonts) { return result; }
            fontApi = app.fonts;
            if (typeof fontApi.allFonts === "undefined" || !fontApi.allFonts) { return result; }
            allFonts = typeof fontApi.allFonts === "function" ? fontApi.allFonts() : fontApi.allFonts;
            if (!allFonts || allFonts.length === undefined) { return result; }
            for (index = 0; index < allFonts.length; index += 1) {
                group = allFonts[index];
                if (group instanceof Array) {
                    for (innerIndex = 0; innerIndex < group.length; innerIndex += 1) {
                        font = group[innerIndex];
                        metadata = fontMetadata(font, includeLocalPath);
                        if (metadata && !catalogName(result, "font_names", metadata.match_name)) {
                            result.fonts.push(metadata);
                            result.font_names.push(metadata.match_name);
                        }
                    }
                } else {
                    metadata = fontMetadata(group, includeLocalPath);
                    if (metadata && !catalogName(result, "font_names", metadata.match_name)) {
                        result.fonts.push(metadata);
                        result.font_names.push(metadata.match_name);
                    }
                }
            }
        } catch (ignoredEnumeration) {
            return { font_names: [], fonts: [] };
        }
        return result;
    }

    function effectMetadata(effect, matchName) {
        var version = null;
        var displayName = "";
        try {
            displayName = String(effect.displayName || effect.name || "");
            if (effect.version !== undefined && effect.version !== null && String(effect.version) !== "") {
                version = String(effect.version);
            } else if (effect.versionString !== undefined && effect.versionString !== null && String(effect.versionString) !== "") {
                version = String(effect.versionString);
            }
        } catch (ignored) {
            return null;
        }
        return {
            match_name: matchName,
            display_name: displayName,
            version: version,
            version_or_hash: version,
            properties: FIXED_EFFECT_TRANSLATIONS[matchName].properties
        };
    }

    function enumerateEffects() {
        var result = { effect_names: [], effects: [], property_schemas: {}, plugin_versions: {} };
        var collection;
        var length;
        var index;
        var effect;
        var matchName;
        var metadata;
        var propertyName;
        try {
            if (!app || typeof app.effects === "undefined" || !app.effects) { return result; }
            collection = app.effects;
            length = Number(collection.length);
            if (!isFinite(length) || length < 0) { return result; }
            for (index = 0; index < length; index += 1) {
                effect = collection[index];
                matchName = String(effect.matchName || effect.match_name || "");
                if (!matchName || !FIXED_EFFECT_TRANSLATIONS[matchName]) { continue; }
                metadata = effectMetadata(effect, matchName);
                if (!metadata) { continue; }
                result.effect_names.push(matchName);
                result.effects.push(metadata);
                result.plugin_versions[matchName] = metadata.version_or_hash;
                for (propertyName in metadata.properties) {
                    if (hasOwn(metadata.properties, propertyName)) {
                        result.property_schemas[propertyName] = metadata.properties[propertyName];
                    }
                }
            }
        } catch (ignoredEnumeration) {
            return { effect_names: [], effects: [], property_schemas: {}, plugin_versions: {} };
        }
        return result;
    }

    function capabilities(includeLocalPaths) {
        var fonts = enumerateFonts(includeLocalPaths === true);
        var effects = enumerateEffects();
        var schemas = fixedPropertySchemas();
        var propertyName;
        for (propertyName in effects.property_schemas) {
            if (hasOwn(effects.property_schemas, propertyName)) {
                schemas[propertyName] = effects.property_schemas[propertyName];
            }
        }
        if (catalogName(effects, "effect_names", "ADBE Fill")) {
            schemas["ADBE Fill Color"] = "color";
        }
        return {
            font_names: fonts.font_names,
            fonts: fonts.fonts,
            effect_names: effects.effect_names,
            effects: effects.effects,
            property_schemas: schemas,
            properties: schemas,
            plugin_versions: effects.plugin_versions
        };
    }

    function heartbeat(includeLocalPaths) {
        var info = versionInfo();
        var catalog = capabilities(includeLocalPaths === true);
        info.capabilities = catalog;
        info.ready = info.major >= 22;
        info.project_open = app.project !== null;
        return info;
    }

    function layerMetadata(layer) {
        var comment = String(layer.comment || "");
        var prefix = "keepframe:layer=";
        var separator = ";source_element_id=";
        var split;
        var instanceId;
        var sourceId;
        if (comment.indexOf(prefix) !== 0) { return null; }
        split = comment.indexOf(separator, prefix.length);
        if (split <= prefix.length) { return null; }
        instanceId = comment.substring(prefix.length, split);
        sourceId = comment.substring(split + separator.length);
        if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(instanceId)) { return null; }
        if (sourceId !== "" && !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/.test(sourceId)) { return null; }
        return {
            layer_instance_id: instanceId,
            source_element_id: sourceId || null
        };
    }

    function layerId(layer) {
        var metadata = layerMetadata(layer);
        return metadata ? metadata.layer_instance_id : "";
    }

    function layerSourceId(layer) {
        var metadata = layerMetadata(layer);
        return metadata ? metadata.source_element_id : null;
    }

    function sessionMarker(projectId, planId, sessionId) {
        if (
            typeof projectId !== "string"
            || !SESSION_ID_PATTERN.test(projectId)
            || typeof planId !== "string"
            || !SESSION_ID_PATTERN.test(planId)
            || typeof sessionId !== "string"
            || !SESSION_ID_PATTERN.test(sessionId)
        ) {
            throw new Error("Keepframe session identity is invalid");
        }
        return SESSION_COMP_MARKER_PREFIX + JSON.stringify([projectId, planId, sessionId]);
    }

    function hasSessionMarker(comp, marker) {
        try {
            return comp instanceof CompItem && marker !== null && String(comp.comment || "") === marker;
        } catch (ignored) {
            return false;
        }
    }

    function compositionIsInProject(comp) {
        var index;
        if (!app.project || !comp) { return false; }
        try {
            for (index = 1; index <= app.project.numItems; index += 1) {
                if (app.project.item(index) === comp) { return true; }
            }
        } catch (ignored) {
            return false;
        }
        return false;
    }

    function findSessionComposition(marker) {
        var found = null;
        var index;
        var item;
        if (!app.project) { return null; }
        for (index = 1; index <= app.project.numItems; index += 1) {
            item = app.project.item(index);
            if (!hasSessionMarker(item, marker)) { continue; }
            if (found) { throw new Error("multiple matching Keepframe session compositions found"); }
            found = item;
        }
        return found;
    }

    function validateCommandScope(kind, payload) {
        var marker;
        var hasProject;
        var hasPlan;
        var hasSession;
        var hasAnyScope;
        assertObject(payload, "command payload");
        hasProject = hasOwn(payload, "project_id");
        hasPlan = hasOwn(payload, "plan_id");
        hasSession = hasOwn(payload, "session_id");
        hasAnyScope = hasProject || hasPlan || hasSession;
        if (kind === "capability_heartbeat" && !hasAnyScope) {
            return true;
        }
        if (!hasProject || !hasPlan || !hasSession) {
            throw new Error("Keepframe command scope is incomplete");
        }
        marker = sessionMarker(payload.project_id, payload.plan_id, payload.session_id);
        if (kind === "create_or_open_project" && SESSION_MARKER === null && SESSION_COMP === null) {
            return false;
        }
        if (SESSION_MARKER !== marker) {
            throw new Error("Keepframe command scope does not match the bound session");
        }
        requireSessionComposition();
        return false;
    }

    function requireSessionComposition() {
        if (!compositionIsInProject(SESSION_COMP) || !hasSessionMarker(SESSION_COMP, SESSION_MARKER)) {
            throw new Error("Keepframe session composition is not bound");
        }
        return SESSION_COMP;
    }

    function findLayer(instanceId) {
        var comp = requireSessionComposition();
        var index = 1;
        var candidate;
        while (index <= comp.numLayers) {
            candidate = comp.layer(index);
            if (layerId(candidate) === instanceId) { return candidate; }
            index += 1;
        }
        return null;
    }

    function requireLayer(instanceId) {
        var layer = findLayer(instanceId);
        if (!layer) { throw new Error("layer is not mapped"); }
        return layer;
    }

    function frameTime(frame) {
        var comp = requireSessionComposition();
        var value = Number(frame);
        if (!isFinite(value) || value < 0 || value > 1000000) { throw new Error("frame is outside bounds"); }
        return value / comp.frameRate;
    }

    function propertyTime(frame, time) {
        var value;
        if (time !== undefined && time !== null) {
            value = Number(time);
            if (!isFinite(value) || value < 0) { throw new Error("operation time is invalid"); }
            return value;
        }
        if (frame === null || frame === undefined) { return null; }
        return frameTime(frame);
    }

    function temporalEase(value, label) {
        var speed;
        var influence;
        if (!(value instanceof Array) || value.length !== 2) {
            throw new Error(label + " is invalid");
        }
        speed = Number(value[0]);
        influence = Number(value[1]);
        if (!isFinite(speed) || Math.abs(speed) > 1000000 || !isFinite(influence) || influence < 0 || influence > 100) {
            throw new Error(label + " is invalid");
        }
        if (typeof KeyframeEase === "undefined") {
            throw new Error("temporal easing is unavailable");
        }
        return new KeyframeEase(speed, influence);
    }

    function applyTemporalEase(property, keyIndex, keyframe) {
        var fallbackIn;
        var fallbackOut;
        var inEase;
        var outEase;
        var dimensions = 1;
        var inEases = [];
        var outEases = [];
        var index;
        if (keyframe.ease_in === undefined && keyframe.ease_out === undefined) { return; }
        if (
            typeof property.setTemporalEaseAtKey !== "function"
            || typeof property.keyInTemporalEase !== "function"
            || typeof property.keyOutTemporalEase !== "function"
        ) {
            throw new Error("temporal easing is unavailable");
        }
        fallbackIn = property.keyInTemporalEase(keyIndex);
        fallbackOut = property.keyOutTemporalEase(keyIndex);
        if (fallbackIn instanceof Array && fallbackIn.length > 0) {
            dimensions = fallbackIn.length;
        } else if (fallbackOut instanceof Array && fallbackOut.length > 0) {
            dimensions = fallbackOut.length;
        }
        inEase = keyframe.ease_in === undefined ? null : temporalEase(keyframe.ease_in, "ease_in");
        outEase = keyframe.ease_out === undefined ? null : temporalEase(keyframe.ease_out, "ease_out");
        for (index = 0; index < dimensions; index += 1) {
            if (inEase) {
                inEases.push(inEase);
            } else if (fallbackIn instanceof Array && fallbackIn[index]) {
                inEases.push(fallbackIn[index]);
            } else {
                throw new Error("temporal easing is unavailable");
            }
            if (outEase) {
                outEases.push(outEase);
            } else if (fallbackOut instanceof Array && fallbackOut[index]) {
                outEases.push(fallbackOut[index]);
            } else {
                throw new Error("temporal easing is unavailable");
            }
        }
        property.setTemporalEaseAtKey(keyIndex, inEases, outEases);
    }

    function setTimedProperty(property, value, frame, time, keyframe) {
        var atTime = propertyTime(frame, time);
        var keyIndex;
        if (
            keyframe
            && (keyframe.ease_in !== undefined || keyframe.ease_out !== undefined)
            && (
                typeof property.nearestKeyIndex !== "function"
                || typeof property.setTemporalEaseAtKey !== "function"
                || typeof property.keyInTemporalEase !== "function"
                || typeof property.keyOutTemporalEase !== "function"
            )
        ) {
            throw new Error("temporal easing is unavailable");
        }
        if (atTime === null) {
            property.setValue(value);
            return;
        }
        property.setValueAtTime(atTime, value);
        if (keyframe && (keyframe.ease_in !== undefined || keyframe.ease_out !== undefined)) {
            keyIndex = property.nearestKeyIndex(atTime);
            applyTemporalEase(property, keyIndex, keyframe);
        }
    }

    function setPropertyAtFrame(layer, propertyName, value, frame, time, keyframe) {
        var property;
        switch (propertyName) {
            case "ADBE Position":
                property = layer.transform.position;
                break;
            case "ADBE Scale":
                property = layer.transform.scale;
                break;
            case "ADBE Rotate Z":
                property = layer.transform.zRotation;
                break;
            case "ADBE Skew":
                property = layer.transform.skew;
                break;
            case "ADBE Skew Axis":
                property = layer.transform.skewAxis;
                break;
            case "ADBE Opacity":
                property = layer.transform.opacity;
                break;
            default:
                throw new Error("property is not in the fixed catalog");
        }
        setTimedProperty(property, value, frame, time, keyframe);
    }

    function setScaleComponentAtFrame(layer, component, value, frame, time, keyframe) {
        var property = layer.transform.scale;
        var current = property.value;
        current[component] = Number(value);
        setTimedProperty(property, current, frame, time, keyframe);
    }

    function setText(layer, text) {
        var textProperty = layer.property("ADBE Text Properties").property("ADBE Text Document");
        var document = textProperty.value;
        document.text = String(text);
        textProperty.setValue(document);
    }

    function normalizedColor(value) {
        var channels = [];
        var index;
        if (typeof value === "string") {
            if (!/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)) {
                throw new Error("color is invalid");
            }
            channels.push(parseInt(value.substring(1, 3), 16) / 255);
            channels.push(parseInt(value.substring(3, 5), 16) / 255);
            channels.push(parseInt(value.substring(5, 7), 16) / 255);
            channels.push(value.length === 9 ? parseInt(value.substring(7, 9), 16) / 255 : 1);
            return channels;
        }
        if (!value || value.length !== 3 && value.length !== 4) {
            throw new Error("color is invalid");
        }
        for (index = 0; index < value.length; index += 1) {
            if (!isFinite(Number(value[index])) || Number(value[index]) < 0 || Number(value[index]) > 1) {
                throw new Error("color channel is invalid");
            }
            channels.push(Number(value[index]));
        }
        if (channels.length === 3) { channels.push(1); }
        return channels;
    }

    function setColor(layer, color, frame, time, keyframe) {
        var effects = layer.property("ADBE Effect Parade");
        var fill = effects.property("ADBE Fill");
        if (!fill) { fill = effects.addProperty("ADBE Fill"); }
        var property = fill.property(2);
        var value = normalizedColor(color);
        setTimedProperty(property, value, frame, time, keyframe);
    }

    function setFont(layer, fontName) {
        var textProperty = layer.property("ADBE Text Properties").property("ADBE Text Document");
        var document = textProperty.value;
        document.font = String(fontName);
        textProperty.setValue(document);
    }

    function setEffect(layer, effectName, properties) {
        var effect;
        switch (effectName) {
            case "ADBE Gaussian Blur 2":
                effect = layer.property("ADBE Effect Parade").property("ADBE Gaussian Blur 2");
                if (!effect) { effect = layer.property("ADBE Effect Parade").addProperty("ADBE Gaussian Blur 2"); }
                if (properties["ADBE Gaussian Blur 2-0001"] !== undefined) {
                    effect.property(1).setValue(Number(properties["ADBE Gaussian Blur 2-0001"]));
                }
                break;
            case "ADBE Fill":
                effect = layer.property("ADBE Effect Parade").property("ADBE Fill");
                if (!effect) { effect = layer.property("ADBE Effect Parade").addProperty("ADBE Fill"); }
                if (properties["ADBE Fill-0002"] !== undefined) {
                    effect.property(2).setValue(normalizedColor(properties["ADBE Fill-0002"]));
                }
                break;
            default:
                throw new Error("effect is not in the fixed catalog");
        }
    }

    function setKeyframes(operation) {
        var layer = requireLayer(operation.layer_instance_id);
        var propertyName = operation.property_name;
        var keyframes = operation.keyframes;
        var index;
        for (index = 0; index < keyframes.length; index += 1) {
            var keyframe = keyframes[index];
            if (propertyName === "ADBE Fill Color") {
                setColor(layer, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Scale X") {
                setScaleComponentAtFrame(layer, 0, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Scale Y") {
                setScaleComponentAtFrame(layer, 1, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Opacity") {
                setPropertyAtFrame(layer, propertyName, Number(keyframe.value) * 100, keyframe.frame, keyframe.time, keyframe);
            } else {
                setPropertyAtFrame(layer, propertyName, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            }
        }
    }

    function applyOperation(operation) {
        if (!operation || !operation.kind) { throw new Error("operation kind is required"); }
        var layer;
        switch (operation.kind) {
            case "set_property":
                layer = requireLayer(operation.layer_instance_id);
                if (operation.property_name === "ADBE Fill Color") {
                    setColor(layer, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "ADBE Scale X") {
                    setScaleComponentAtFrame(layer, 0, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "ADBE Scale Y") {
                    setScaleComponentAtFrame(layer, 1, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "ADBE Opacity") {
                    setPropertyAtFrame(layer, operation.property_name, Number(operation.value) * 100, operation.frame, operation.time);
                } else {
                    setPropertyAtFrame(layer, operation.property_name, operation.value, operation.frame, operation.time);
                }
                break;
            case "set_transform":
            case "set_layer_transform":
                layer = requireLayer(operation.layer_instance_id);
                if (operation.property_name === "position") {
                    setPropertyAtFrame(layer, "ADBE Position", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale") {
                    setPropertyAtFrame(layer, "ADBE Scale", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale_x") {
                    setScaleComponentAtFrame(layer, 0, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale_y") {
                    setScaleComponentAtFrame(layer, 1, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "rotation") {
                    setPropertyAtFrame(layer, "ADBE Rotate Z", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "skew" || operation.property_name === "skew_x") {
                    setPropertyAtFrame(layer, "ADBE Skew", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "skew_y") {
                    setPropertyAtFrame(layer, "ADBE Skew Axis", operation.value, operation.frame, operation.time);
                } else {
                    throw new Error("transform property is not in the fixed catalog");
                }
                break;
            case "set_opacity":
            case "set_layer_opacity":
                layer = requireLayer(operation.layer_instance_id);
                setPropertyAtFrame(layer, "ADBE Opacity", operation.opacity * 100, operation.frame, operation.time);
                break;
            case "set_color":
            case "set_layer_color":
                layer = requireLayer(operation.layer_instance_id);
                setColor(layer, operation.color, operation.frame, operation.time);
                break;
            case "set_keyframes":
            case "set_property_keyframes":
                setKeyframes(operation);
                break;
            case "set_text":
            case "set_layer_text":
                layer = requireLayer(operation.layer_instance_id);
                setText(layer, operation.text);
                if (operation.font_name !== undefined && operation.font_name !== null) { setFont(layer, operation.font_name); }
                break;
            case "set_font":
            case "set_layer_font":
                layer = requireLayer(operation.layer_instance_id);
                setFont(layer, operation.font_name);
                break;
            case "set_effect":
            case "add_effect":
            case "set_layer_effect":
                layer = requireLayer(operation.layer_instance_id);
                setEffect(layer, operation.effect_name, operation.properties || {});
                break;
            case "add_layer":
                addMappedLayer(operation);
                break;
            case "remove_layer":
                layer = requireLayer(operation.layer_instance_id);
                layer.remove();
                break;
            case "set_visibility":
                layer = requireLayer(operation.layer_instance_id);
                layer.enabled = operation.visible === true;
                if (operation.frame_start !== undefined && operation.frame_start !== null) {
                    layer.inPoint = frameTime(operation.frame_start);
                }
                if (operation.frame_end !== undefined && operation.frame_end !== null) {
                    layer.outPoint = frameTime(operation.frame_end);
                }
                break;
            default:
                throw new Error("operation kind is not in the fixed catalog");
        }
    }

    function addMappedLayer(operation) {
        var comp = requireSessionComposition();
        var layer;
        switch (operation.layer_type) {
            case "null":
                layer = comp.layers.addNull();
                break;
            case "text":
                layer = comp.layers.addText(operation.name);
                break;
            case "solid":
                layer = comp.layers.addSolid([0, 0, 0], operation.name, comp.width, comp.height, 1);
                break;
            default:
                throw new Error("layer type is not in the fixed catalog");
        }
        layer.comment = "keepframe:layer=" + operation.layer_instance_id + ";source_element_id=" + (operation.source_element_id || "");
        if (operation.parent_instance_id) {
            var parent = findLayer(operation.parent_instance_id);
            if (!parent) { throw new Error("parent layer is not mapped"); }
            layer.parent = parent;
        }
    }

    function hasOwn(object, key) {
        return Object.prototype.hasOwnProperty.call(object, key);
    }

    function assertObject(object, label) {
        if (!object || typeof object !== "object" || object instanceof Array) {
            throw new Error(label + " must be an object");
        }
    }

    function assertKeys(object, keys, label) {
        var key;
        for (key in object) {
            if (hasOwn(object, key) && !hasOwn(keys, key)) {
                throw new Error(label + " contains an unknown field");
            }
        }
    }

    function assertString(value, label) {
        if (typeof value !== "string" || value.length > 4096) {
            throw new Error(label + " must be a bounded string");
        }
        if (/^(https?|file|javascript|data):/i.test(value) || /^(\/|\\|\.\/|\.\.\/)/.test(value) || /^[A-Za-z]:[\\/]/.test(value) || value.indexOf("\\") >= 0 || value.indexOf("/../") >= 0) {
            throw new Error(label + " must not contain a path or URL");
        }
    }

    function assertSafeValue(value, depth) {
        var key;
        var index;
        if (depth > 64) { throw new Error("operation value is too deeply nested"); }
        if (typeof value === "number") {
            if (!isFinite(value) || Math.abs(value) > 1000000) { throw new Error("operation number is outside bounds"); }
            return;
        }
        if (typeof value === "string") {
            assertString(value, "operation value");
            return;
        }
        if (value === null || typeof value === "boolean") { return; }
        if (value instanceof Array) {
            for (index = 0; index < value.length; index += 1) { assertSafeValue(value[index], depth + 1); }
            return;
        }
        assertObject(value, "operation value");
        for (key in value) {
            if (hasOwn(value, key)) {
                assertString(key, "operation key");
                if (hasOwn({ argv: true, code: true, command: true, destination: true, executable: true, expression: true, extend_script: true, file: true, file_path: true, filepath: true, jsx: true, local_path: true, output_path: true, path: true, project_path: true, raw_code: true, script: true, source_path: true, uri: true, url: true }, key.toLowerCase())) {
                    throw new Error("operation key is not allowed");
                }
                assertSafeValue(value[key], depth + 1);
            }
        }
    }

    function assertTemporalEase(value, label) {
        if (!(value instanceof Array) || value.length !== 2) {
            throw new Error(label + " is invalid");
        }
        if (
            typeof value[0] !== "number"
            || !isFinite(value[0])
            || Math.abs(value[0]) > 1000000
            || typeof value[1] !== "number"
            || !isFinite(value[1])
            || value[1] < 0
            || value[1] > 100
        ) {
            throw new Error(label + " is invalid");
        }
    }

    function capabilityProperty(payload, propertyName, value) {
        var catalog = payload.approved_capabilities;
        var schema;
        var requiredEffect;
        assertObject(catalog, "approved_capabilities");
        assertObject(catalog.properties, "approved property catalog");
        if (!hasOwn(catalog.properties, propertyName)) {
            throw new Error("property is not in the approved catalog");
        }
        requiredEffect = { "ADBE Fill Color": "ADBE Fill", "ADBE Fill-0002": "ADBE Fill" }[propertyName];
        if (requiredEffect !== undefined && !catalogName(catalog, "effects", requiredEffect)) {
            throw new Error("property requires an approved effect");
        }
        if (propertyName === "ADBE Opacity" && (typeof value !== "number" || !isFinite(value) || value < 0 || value > 1)) {
            throw new Error("opacity is outside the unit interval");
        }
        schema = String(catalog.properties[propertyName]).toLowerCase();
        if (schema === "number" || schema === "float") {
            if (typeof value !== "number" || !isFinite(value)) { throw new Error("property value type is invalid"); }
        } else if (schema === "boolean" || schema === "bool") {
            if (typeof value !== "boolean") { throw new Error("property value type is invalid"); }
        } else if (schema === "string" || schema === "str" || schema === "enum") {
            assertString(value, "property value");
        } else if (schema === "color" || schema === "rgba") {
            if (typeof value === "string") {
                if (!/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(value)) { throw new Error("property color is invalid"); }
            } else if (!(value instanceof Array) || (value.length !== 3 && value.length !== 4)) {
                throw new Error("property color is invalid");
            } else {
                var channelIndex;
                for (channelIndex = 0; channelIndex < value.length; channelIndex += 1) {
                    if (typeof value[channelIndex] !== "number" || !isFinite(value[channelIndex]) || value[channelIndex] < 0 || value[channelIndex] > 1) { throw new Error("property color channel is invalid"); }
                }
            }
        } else {
            var dimensions = { vec2: 2, vector2: 2, vec3: 3, vector3: 3, vec4: 4, vector4: 4 };
            if (dimensions[schema] === undefined || !(value instanceof Array) || value.length !== dimensions[schema]) {
                throw new Error("property schema is unavailable");
            }
        }
    }

    function catalogName(catalog, field, value) {
        var items = catalog[field] || [];
        var index;
        for (index = 0; index < items.length; index += 1) {
            if (items[index] === value) { return true; }
        }
        return false;
    }

    function assertEffectPropertyOperation(kind, propertyName) {
        if (
            (propertyName === "ADBE Gaussian Blur 2-0001" || propertyName === "ADBE Fill-0002")
            && kind !== "set_effect"
            && kind !== "add_effect"
            && kind !== "set_layer_effect"
        ) {
            throw new Error("effect properties require set_effect");
        }
    }

    function assertDirectProperty(propertyName) {
        if (propertyName !== "ADBE Fill Color" && !hasOwn(fixedPropertySchemas(), propertyName)) {
            throw new Error("property is not in the fixed catalog");
        }
    }

    function validateOperation(payload, operation, frameLimit) {
        assertObject(operation, "operation");
        assertString(operation.kind, "operation kind");
        assertString(operation.layer_instance_id, "layer_instance_id");
        switch (operation.kind) {
            case "set_property":
                assertKeys(operation, { kind: true, layer_instance_id: true, property_name: true, value: true, frame: true, time: true }, "set_property");
                assertString(operation.property_name, "property_name");
                assertEffectPropertyOperation(operation.kind, operation.property_name);
                assertDirectProperty(operation.property_name);
                assertSafeValue(operation.value, 0);
                capabilityProperty(payload, operation.property_name, operation.value);
                break;
            case "set_transform":
            case "set_layer_transform":
                assertKeys(operation, { kind: true, layer_instance_id: true, property_name: true, value: true, frame: true, time: true }, "set_transform");
                if (!/^(position|scale|scale_x|scale_y|rotation|skew|skew_x|skew_y)$/.test(operation.property_name)) { throw new Error("transform property is invalid"); }
                assertSafeValue(operation.value, 0);
                capabilityProperty(payload, { position: "ADBE Position", scale: "ADBE Scale", scale_x: "ADBE Scale X", scale_y: "ADBE Scale Y", rotation: "ADBE Rotate Z", skew: "ADBE Skew", skew_x: "ADBE Skew", skew_y: "ADBE Skew Axis" }[operation.property_name], operation.value);
                break;
            case "set_opacity":
            case "set_layer_opacity":
                assertKeys(operation, { kind: true, layer_instance_id: true, opacity: true, frame: true, time: true }, "set_opacity");
                if (typeof operation.opacity !== "number" || !isFinite(operation.opacity) || operation.opacity < 0 || operation.opacity > 1) { throw new Error("opacity is invalid"); }
                capabilityProperty(payload, "ADBE Opacity", operation.opacity);
                break;
            case "set_color":
            case "set_layer_color":
                assertKeys(operation, { kind: true, layer_instance_id: true, color: true, frame: true, time: true }, "set_color");
                assertSafeValue(operation.color, 0);
                capabilityProperty(payload, "ADBE Fill Color", operation.color);
                break;
            case "set_keyframes":
            case "set_property_keyframes":
                assertKeys(operation, { kind: true, layer_instance_id: true, property_name: true, keyframes: true }, "set_keyframes");
                assertString(operation.property_name, "property_name");
                assertEffectPropertyOperation(operation.kind, operation.property_name);
                assertDirectProperty(operation.property_name);
                var keyframeLimit = frameLimit === null ? MAX_KEYFRAMES : frameLimit;
                if (!(operation.keyframes instanceof Array) || operation.keyframes.length === 0 || operation.keyframes.length > keyframeLimit) { throw new Error("keyframes are outside bounds"); }
                var keyframeIndex;
                for (keyframeIndex = 0; keyframeIndex < operation.keyframes.length; keyframeIndex += 1) {
                    var keyframe = operation.keyframes[keyframeIndex];
                    assertObject(keyframe, "keyframe");
                    assertKeys(keyframe, { frame: true, value: true, time: true, ease_in: true, ease_out: true }, "keyframe");
                    if (typeof keyframe.frame !== "number" || !isFinite(keyframe.frame) || keyframe.frame < 0 || Math.floor(keyframe.frame) !== keyframe.frame) { throw new Error("keyframe frame is invalid"); }
                    assertSafeValue(keyframe.value, 0);
                    if (keyframe.ease_in !== undefined) { assertTemporalEase(keyframe.ease_in, "ease_in"); }
                    if (keyframe.ease_out !== undefined) { assertTemporalEase(keyframe.ease_out, "ease_out"); }
                    capabilityProperty(payload, operation.property_name, keyframe.value);
                }
                break;
            case "set_text":
            case "set_layer_text":
                assertKeys(operation, { kind: true, layer_instance_id: true, text: true, font_name: true }, "set_text");
                assertString(operation.text, "text");
                if (operation.font_name !== undefined && operation.font_name !== null && !catalogName(payload.approved_capabilities, "fonts", operation.font_name)) { throw new Error("font is not in the approved catalog"); }
                break;
            case "set_font":
            case "set_layer_font":
                assertKeys(operation, { kind: true, layer_instance_id: true, font_name: true }, "set_font");
                if (!catalogName(payload.approved_capabilities, "fonts", operation.font_name)) { throw new Error("font is not in the approved catalog"); }
                break;
            case "set_effect":
            case "add_effect":
            case "set_layer_effect":
                assertKeys(operation, { kind: true, layer_instance_id: true, effect_name: true, properties: true }, "set_effect");
                if (!hasOwn(FIXED_EFFECT_TRANSLATIONS, operation.effect_name)) { throw new Error("effect is not in the fixed catalog"); }
                if (!catalogName(payload.approved_capabilities, "effects", operation.effect_name)) { throw new Error("effect is not in the approved catalog"); }
                assertObject(operation.properties, "effect properties");
                var propertyName;
                for (propertyName in operation.properties) {
                    if (hasOwn(operation.properties, propertyName)) {
                        if (!hasOwn(FIXED_EFFECT_TRANSLATIONS[operation.effect_name].properties, propertyName)) { throw new Error("effect property does not match the fixed effect translation"); }
                        assertSafeValue(operation.properties[propertyName], 0);
                        capabilityProperty(payload, propertyName, operation.properties[propertyName]);
                    }
                }
                break;
            case "add_layer":
                assertKeys(operation, { kind: true, layer_instance_id: true, layer_type: true, name: true, source_element_id: true, parent_instance_id: true }, "add_layer");
                if (!/^(text|solid|null)$/.test(operation.layer_type)) { throw new Error("layer type is invalid"); }
                assertString(operation.name, "layer name");
                break;
            case "remove_layer":
                assertKeys(operation, { kind: true, layer_instance_id: true }, "remove_layer");
                break;
            case "set_visibility":
                assertKeys(operation, { kind: true, layer_instance_id: true, visible: true, frame_start: true, frame_end: true }, "set_visibility");
                if (typeof operation.visible !== "boolean") { throw new Error("visibility must be boolean"); }
                break;
            default:
                throw new Error("operation kind is not in the fixed catalog");
        }
    }

    function approximatelyEqual(left, right) {
        return Math.abs(left - right) <= 0.000001;
    }

    function validateAuthoritativeBounds(comp, sceneFrameCount, duration, layerCount) {
        var expectedFrameCount = comp.duration * comp.frameRate;
        if (typeof sceneFrameCount !== "number" || !isFinite(sceneFrameCount) || sceneFrameCount < 1 || Math.floor(sceneFrameCount) !== sceneFrameCount) {
            throw new Error("scene frame count is invalid");
        }
        if (typeof duration !== "number" || !isFinite(duration) || duration <= 0) {
            throw new Error("scene duration is invalid");
        }
        if (typeof layerCount !== "number" || !isFinite(layerCount) || layerCount < 0 || Math.floor(layerCount) !== layerCount) {
            throw new Error("layer count is invalid");
        }
        if (layerCount > 1000) { throw new Error("layer count exceeds 1000"); }
        if (!isFinite(expectedFrameCount) || !approximatelyEqual(sceneFrameCount, expectedFrameCount)) {
            throw new Error("scene frame count does not match the bound composition");
        }
        if (!approximatelyEqual(duration, comp.duration)) {
            throw new Error("scene duration does not match the bound composition");
        }
        if (comp.numLayers !== layerCount) {
            throw new Error("layer count does not match the bound composition");
        }
    }

    function validateTiming(operation, frameLimit, duration) {
        var frame;
        var keyframeIndex;
        function checkFrame(value) {
            if (value === undefined || value === null) { return; }
            if (typeof value !== "number" || !isFinite(value) || value < 0 || Math.floor(value) !== value) { throw new Error("operation frame is invalid"); }
            if (value >= frameLimit) { throw new Error("operation frame is outside the scene"); }
        }
        function checkTime(value) {
            if (value === undefined || value === null) { return; }
            if (typeof value !== "number" || !isFinite(value) || value < 0) { throw new Error("operation time is invalid"); }
            if (value > duration) { throw new Error("operation time is outside the comp"); }
        }
        frame = operation.frame;
        checkFrame(frame);
        checkFrame(operation.frame_start);
        checkFrame(operation.frame_end);
        checkTime(operation.time);
        if (operation.keyframes instanceof Array) {
            for (keyframeIndex = 0; keyframeIndex < operation.keyframes.length; keyframeIndex += 1) {
                checkFrame(operation.keyframes[keyframeIndex].frame);
                checkTime(operation.keyframes[keyframeIndex].time);
            }
        }
    }

    function validatedBatch(payload, frameLimit, duration) {
        var batch;
        var operations;
        var index;
        assertObject(payload, "apply_operation_batch payload");
        assertObject(payload.batch, "batch");
        assertObject(payload.approved_capabilities, "approved_capabilities");
        batch = payload.batch;
        if (batch.capability_digest !== payload.approved_capabilities.digest) { throw new Error("capability digest does not match"); }
        operations = batch.operations;
        if (!(operations instanceof Array) || operations.length > 128) { throw new Error("operation batch is outside bounds"); }
        if (JSON.stringify(payload).length > MAX_JSON_BYTES) { throw new Error("operation batch exceeds 1 MiB"); }
        for (index = 0; index < operations.length; index += 1) {
            validateOperation(payload, operations[index], frameLimit);
            validateTiming(operations[index], frameLimit, duration);
        }
        return batch;
    }

    function applyBatch(payload) {
        var comp;
        var sceneFrameCount;
        var duration;
        var layerCount;
        var batch;
        var operations;
        var index;
        assertObject(payload, "apply_operation_batch payload");
        comp = requireSessionComposition();
        sceneFrameCount = payload.scene_frame_count;
        duration = payload.duration;
        layerCount = payload.layer_count;
        validateAuthoritativeBounds(comp, sceneFrameCount, duration, layerCount);
        batch = validatedBatch(payload, sceneFrameCount, duration);
        operations = batch.operations;
        for (index = 0; index < operations.length; index += 1) {
            if (operations[index].kind === "add_layer") { layerCount += 1; }
            if (operations[index].kind === "remove_layer") { layerCount = Math.max(0, layerCount - 1); }
            if (layerCount > 1000) { throw new Error("layer count exceeds 1000"); }
        }
        activeBatch = true;
        var stopped = false;
        app.beginUndoGroup("Keepframe operation batch");
        try {
            for (index = 0; index < operations.length; index += 1) {
                applyOperation(operations[index]);
            }
            stopped = STOP_FILE.exists;
        } finally {
            app.endUndoGroup();
            activeBatch = false;
        }
        removeStop();
        return { applied: operations.length, stop_requested: stopped };
    }

    function createOrOpenProject(payload) {
        var marker = sessionMarker(payload.project_id, payload.plan_id, payload.session_id);
        var info = heartbeat(false);
        var comp = null;
        var width;
        var height;
        var frameRate;
        var duration;
        if (!info.ready) { throw new Error("After Effects 2022 or newer is required"); }
        if (SESSION_MARKER === marker && SESSION_COMP && compositionIsInProject(SESSION_COMP) && hasSessionMarker(SESSION_COMP, marker)) {
            comp = SESSION_COMP;
        } else if (app.project) {
            comp = findSessionComposition(marker);
        }
        if (comp) {
            SESSION_MARKER = marker;
            SESSION_COMP = comp;
            comp.openInViewer();
            return { opened: true, project_open: true, heartbeat: heartbeat(false) };
        }
        if (app.project) {
            throw new Error("refusing to reuse an unrelated After Effects project");
        }
        app.newProject();
        width = Number(payload.width || 1920);
        height = Number(payload.height || 1080);
        frameRate = Number(payload.frame_rate || 30);
        duration = Number(payload.duration || 5);
        if (!isFinite(width) || width < 1 || !isFinite(height) || height < 1 || !isFinite(frameRate) || frameRate <= 0 || !isFinite(duration) || duration <= 0) {
            throw new Error("session composition settings are invalid");
        }
        app.beginUndoGroup("Keepframe create session composition");
        try {
            comp = app.project.items.addComp("Keepframe", width, height, 1, duration, frameRate);
            comp.comment = marker;
            if (!hasSessionMarker(comp, marker)) { throw new Error("cannot establish Keepframe session marker"); }
            SESSION_MARKER = marker;
            SESSION_COMP = comp;
        } finally {
            app.endUndoGroup();
        }
        comp.openInViewer();
        return { opened: true, project_open: true, heartbeat: heartbeat(false) };
    }

    function importServerAsset(payload) {
        requireSessionComposition();
        var assetId = String(payload.asset_id || "");
        var file;
        var footage;
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{7,255}$/.test(assetId)) { throw new Error("asset id is invalid"); }
        file = File(ASSET_DIR.fsName + "/" + assetId);
        if (!file.exists) { throw new Error("server-issued asset is unavailable"); }
        app.beginUndoGroup("Keepframe import server asset");
        try {
            footage = app.project.importFile(new ImportOptions(file));
        } finally {
            app.endUndoGroup();
        }
        return { imported: true, asset_id: assetId, item_name: footage.name };
    }


    function inspectLayers() {
        var rows = [];
        var comp = requireSessionComposition();
        var index = 1;
        var layer;
        while (index <= comp.numLayers) {
            layer = comp.layer(index);
            rows.push({
                layer_instance_id: layerId(layer),
                name: layer.name,
                index: index,
                in_point: layer.inPoint,
                out_point: layer.outPoint,
                source_element_id: layerSourceId(layer)
            });
            index += 1;
        }
        return { layers: rows, heartbeat: heartbeat(false) };
    }

    function saveCheckpoint(payload) {
        requireSessionComposition();
        var index = Number(payload.index || 0);
        if (!isFinite(index) || index < 0 || index > 1000000 || Math.floor(index) !== index) {
            throw new Error("checkpoint index is invalid");
        }
        var file = File(CHECKPOINT_DIR.fsName + "/checkpoint-" + String(index) + ".aep");
        app.project.save(file);
        return { saved: true, index: index, filename: file.name };
    }

    function render(kind, payload) {
        requireSessionComposition();
        var file = File(RENDER_DIR.fsName + "/" + kind + ".aep");
        app.project.save(file);
        return { saved: true, kind: kind, filename: file.name, frame: payload.frame || 0 };
    }

    function packageProject() {
        requireSessionComposition();
        var file = File(RENDER_DIR.fsName + "/project.aep");
        app.project.save(file);
        return { saved: true, filename: file.name };
    }

    function completedResult(command) {
        var files = COMPLETED_DIR.getFiles();
        var index;
        var filename;
        var match;
        var record;
        for (index = 0; index < files.length; index += 1) {
            if (!(files[index] instanceof File)) { continue; }
            filename = String(files[index].name || "");
            match = /^([A-Za-z0-9][A-Za-z0-9_.-]{0,255})--([A-Za-z0-9][A-Za-z0-9_.-]{0,255})\.json$/.exec(filename);
            if (!match || match[1] !== command.command_id || match[2] !== command.nonce) { continue; }
            record = readJson(files[index]);
            if (record === null) { throw new Error("completed bridge record is invalid"); }
            if (record.schema_version !== BRIDGE_SCHEMA_VERSION || record.command_id !== command.command_id || record.nonce !== command.nonce) {
                return resultRecord(command, false, {}, "completed bridge record metadata conflicts");
            }
            if (
                record.nonce !== command.nonce
                || record.kind !== command.kind
                || record.payload_digest !== command.payload_digest
            ) {
                return resultRecord(command, false, {}, "completed command metadata conflicts");
            }
            return record;
        }
        return null;
    }

    function attemptFile(command) {
        return File(
            INFLIGHT_DIR.fsName + "/" + command.command_id + "--" + command.nonce + ".json"
        );
    }

    function inflightAttempt(command) {
        var files = INFLIGHT_DIR.getFiles();
        var index;
        var filename;
        var match;
        var record;
        for (index = 0; index < files.length; index += 1) {
            if (!(files[index] instanceof File)) { continue; }
            filename = String(files[index].name || "");
            match = /^([A-Za-z0-9][A-Za-z0-9_.-]{0,255})--([A-Za-z0-9][A-Za-z0-9_.-]{0,255})\.json$/.exec(filename);
            if (!match || match[1] !== command.command_id) { continue; }
            if (match[2] !== command.nonce) {
                throw new Error("inflight command nonce conflicts");
            }
            record = readJson(files[index]);
            if (record === null) { throw new Error("inflight bridge attempt is invalid"); }
            if (
                record.schema_version !== BRIDGE_SCHEMA_VERSION
                || record.command_id !== command.command_id
                || record.nonce !== command.nonce
                || record.kind !== command.kind
                || record.payload_digest !== command.payload_digest
            ) {
                throw new Error("inflight command metadata conflicts");
            }
            return record;
        }
        return null;
    }

    function markAttempt(command) {
        var record = {
            schema_version: BRIDGE_SCHEMA_VERSION,
            command_id: command.command_id,
            nonce: command.nonce,
            kind: command.kind,
            payload_digest: command.payload_digest
        };
        if (!writeImmutable(attemptFile(command), record)) {
            throw new Error("cannot persist immutable command attempt");
        }
    }

    function removeAttempt(command) {
        var file = attemptFile(command);
        try {
            if (file.exists) { file.remove(); }
        } catch (ignored) {
            /* Completed records remain the durable replay authority. */
        }
    }

    function indeterminateResult(command) {
        return resultRecord(
            command,
            false,
            {},
            "command outcome is indeterminate after panel interruption"
        );
    }

    function markCompleted(record) {
        var file = File(
            COMPLETED_DIR.fsName + "/" + record.command_id + "--" + record.nonce + ".json"
        );
        if (!writeImmutable(file, record)) {
            throw new Error("cannot persist immutable completed command");
        }
    }

    function resultRecord(command, ok, value, error) {
        var record = {
            schema_version: BRIDGE_SCHEMA_VERSION,
            command_id: command.command_id,
            nonce: command.nonce,
            kind: command.kind,
            payload_digest: command.payload_digest,
            ok: ok,
            result: value || {},
            result_digest: null
        };
        if (error) { record.error = String(error); }
        return record;
    }


    function dispatchCommand(command) {
        var includeLocalPaths = validateCommandScope(command.kind, command.payload || {});
        switch (command.kind) {
            case "capability_heartbeat":
                return heartbeat(includeLocalPaths);
            case "create_or_open_project":
                return createOrOpenProject(command.payload || {});
            case "import_server_asset":
                return importServerAsset(command.payload || {});
            case "apply_operation_batch":
                return applyBatch(command.payload || {});
            case "inspect_mapped_layers":
                return inspectLayers();
            case "save_checkpoint":
                return saveCheckpoint(command.payload || {});
            case "render_preview":
                return render("preview", command.payload || {});
            case "render_final":
                return render("final", command.payload || {});
            case "package_project":
                return packageProject();
            default:
                throw new Error("command kind is not in the fixed catalog");
        }
    }

    function KeepframeBridge_poll() {
        ensureFolders();
        var command = readJson(COMMAND_FILE);
        var cached;
        var response;
        var dispatched = false;
        if (!command || command.schema_version !== BRIDGE_SCHEMA_VERSION) { return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(String(command.command_id || "")) || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(String(command.nonce || "")) || !command.kind || !command.payload_digest) { return; }
        cached = completedResult(command);
        if (cached) {
            writeAtomic(RESULT_FILE, cached);
            return;
        }
        try {
            validateCommandScope(command.kind, command.payload || {});
            if (inflightAttempt(command)) {
                response = indeterminateResult(command);
            } else {
                markAttempt(command);
                dispatched = true;
                response = resultRecord(command, true, dispatchCommand(command), null);
            }
        } catch (error) {
            response = resultRecord(command, false, {}, error);
        }
        /* Journal before publishing; a failed journal leaves the attempt as the
         * durable indeterminate barrier, so recovery never dispatches again. */
        try {
            markCompleted(response);
            writeAtomic(RESULT_FILE, response);
            if (dispatched) { removeAttempt(command); }
        } catch (writeError) {
            /* Keep the attempt marker whenever completed persistence is not
             * confirmed; a later poll must fail closed. */
        }
    }

    function buildPanel(thisObj) {
        var window = (thisObj instanceof Panel) ? thisObj : new Window("palette", "Keepframe AE Bridge", undefined, { resizeable: true });
        if (!window) { return window; }
        window.orientation = "column";
        window.alignChildren = ["fill", "top"];
        var status = window.add("statictext", undefined, "Keepframe: waiting for heartbeat");
        var heartbeatButton = window.add("button", undefined, "Heartbeat");
        heartbeatButton.onClick = function () {
            var value = heartbeat(false);
            status.text = "AE " + value.version + (value.ready ? " ready" : " unsupported");
        };
        var closeButton = window.add("button", undefined, "Close");
        closeButton.onClick = function () { window.close(); };
        window.onClose = function () { pollScheduled = false; };
        return window;
    }

    $.global.KeepframeBridge_poll = KeepframeBridge_poll;
    ensureFolders();
    panelWindow = buildPanel(this);
    if (panelWindow) {
        if (panelWindow instanceof Window) { panelWindow.show(); }
        if (!pollScheduled) {
            pollScheduled = true;
            app.scheduleTask("KeepframeBridge_poll()", 500, true);
        }
    }
}(this));
