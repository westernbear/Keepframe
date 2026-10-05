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
    var CHECKPOINT_DIR = null;
    var RENDER_DIR = null;
    var MEDIA_DIR = null;
    var PACKAGE_DIR = null;
    var MAX_JSON_BYTES = 1048576;
    var MAX_KEYFRAMES = 1000000;
    var MAX_FINAL_FRAMES = 100000;
    var MAX_FINAL_TOTAL_PIXELS = 10000000000;
    var SESSION_COMP_MARKER_PREFIX = "keepframe:session:v1:";
    var SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
    var SHA256_PATTERN = /^[0-9a-f]{64}$/;
    var ASSET_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/;
    var SESSION_COMP = null;
    var SESSION_MARKER = null;
    var panelWindow = null;
    var pollScheduled = false;
    var activeBatch = false;

    function ensureFolders() {
        if (!BRIDGE_ROOT.exists && !BRIDGE_ROOT.create()) { throw new Error("Keepframe bridge root is unavailable"); }
        if (!COMPLETED_DIR.exists && !COMPLETED_DIR.create()) { throw new Error("Keepframe completed directory is unavailable"); }
        if (!INFLIGHT_DIR.exists && !INFLIGHT_DIR.create()) { throw new Error("Keepframe inflight directory is unavailable"); }
        if (CHECKPOINT_DIR !== null && !CHECKPOINT_DIR.exists && !CHECKPOINT_DIR.create()) { throw new Error("Keepframe checkpoint directory is unavailable"); }
        if (RENDER_DIR !== null && !RENDER_DIR.exists && !RENDER_DIR.create()) { throw new Error("Keepframe render directory is unavailable"); }
        if (PACKAGE_DIR !== null && !PACKAGE_DIR.exists && !PACKAGE_DIR.create()) { throw new Error("Keepframe package directory is unavailable"); }
        if (MEDIA_DIR !== null && !MEDIA_DIR.exists && !MEDIA_DIR.create()) { throw new Error("Keepframe media directory is unavailable"); }
    }

    function bindSessionDirectories(projectId, planId, sessionId) {
        var marker = sessionMarker(projectId, planId, sessionId);
        var scopeRoot;
        var projectsRoot;
        var projectRoot;
        var planRoot;
        var plansRoot;
        var sessionsRoot;
        var sessionRoot;
        if (
            typeof projectId !== "string"
            || typeof planId !== "string"
            || typeof sessionId !== "string"
            || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(projectId)
            || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(planId)
            || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(sessionId)
        ) {
            throw new Error("Keepframe scope identity is not path-safe");
        }
        if (SESSION_MARKER !== null && SESSION_MARKER !== marker) {
            throw new Error("Keepframe scope does not match the bound session");
        }
        ensureFolders();
        projectsRoot = Folder(BRIDGE_ROOT.fsName + "/projects");
        projectRoot = Folder(projectsRoot.fsName + "/" + projectId);
        plansRoot = Folder(projectRoot.fsName + "/plans");
        planRoot = Folder(plansRoot.fsName + "/" + planId);
        sessionsRoot = Folder(planRoot.fsName + "/sessions");
        sessionRoot = Folder(sessionsRoot.fsName + "/" + sessionId);
        scopeRoot = sessionRoot;
        if (!projectsRoot.exists && !projectsRoot.create()) { throw new Error("Keepframe project root is unavailable"); }
        if (!projectRoot.exists && !projectRoot.create()) { throw new Error("Keepframe project scope is unavailable"); }
        if (!plansRoot.exists && !plansRoot.create()) { throw new Error("Keepframe plans scope is unavailable"); }
        if (!planRoot.exists && !planRoot.create()) { throw new Error("Keepframe plan scope is unavailable"); }
        if (!sessionsRoot.exists && !sessionsRoot.create()) { throw new Error("Keepframe sessions scope is unavailable"); }
        if (!sessionRoot.exists && !sessionRoot.create()) { throw new Error("Keepframe session scope is unavailable"); }
        CHECKPOINT_DIR = Folder(scopeRoot.fsName + "/checkpoints");
        RENDER_DIR = Folder(scopeRoot.fsName + "/renders");
        PACKAGE_DIR = Folder(scopeRoot.fsName + "/package");
        MEDIA_DIR = Folder(PACKAGE_DIR.fsName + "/collected_media");
        ensureFolders();
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

    function modelLayersAvailable() {
        var parts = String(app.version).split(".");
        var major = parseInt(parts[0], 10);
        var minor = parseInt(parts[1] || "0", 10);
        return major > 24 || (major === 24 && minor >= 1);
    }

    function advanced3DRenderer(comp) {
        var renderers = comp.renderers || [];
        var index;
        var name;
        for (index = 0; index < renderers.length; index += 1) {
            name = String(renderers[index]).toLowerCase();
            if (name.indexOf("advanced") !== -1 && name.indexOf("3d") !== -1) { return renderers[index]; }
        }
        throw new Error("Advanced 3D renderer is unavailable");
    }

    var FIXED_EFFECT_TRANSLATIONS = {
        "ADBE Gaussian Blur 2": {
            properties: { "ADBE Gaussian Blur 2-0001": "number" }
        },
        "ADBE Fill": {
            properties: { "ADBE Fill-0002": "color" }
        },
        "ADBE Linear Wipe": {
            properties: { "ADBE Linear Wipe-0001": "number", "ADBE Linear Wipe-0002": "number", "ADBE Linear Wipe-0003": "number" }
        }
    };

    function fixedPropertySchemas() {
        return {
            "ADBE Position": "vec2",
            "ADBE Position X": "number",
            "ADBE Position Y": "number",
            "ADBE Scale": "vec2",
            "ADBE Scale X": "number",
            "ADBE Scale Y": "number",
            "ADBE Rotate Z": "number",
            "ADBE Rotate X": "number",
            "ADBE Rotate Y": "number",
            "ADBE Skew": "number",
            "ADBE Skew Axis": "number",
            "ADBE Anchor Point": "vec2",
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
            model_layers: modelLayersAvailable(),
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

    function nativeLayerId(layer) {
        var value = layer && layer.id;
        if (
            typeof value !== "number"
            || !isFinite(value)
            || value < 1
            || value > 1000000
            || Math.floor(value) !== value
        ) {
            throw new Error("native layer id is invalid");
        }
        return value;
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
        bindSessionDirectories(payload.project_id, payload.plan_id, payload.session_id);
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

    function propertyDimensionCount(property) {
        var current;
        try {
            current = property.value;
        } catch (ignored) {
            current = null;
        }
        if (current instanceof Array && current.length > 0) { return current.length; }
        return 1;
    }

    function temporalEase(value, label, dimensions, speedScale) {
        var pairs = [];
        var pair;
        var speed;
        var influence;
        var index;
        speedScale = speedScale === undefined ? 1 : Number(speedScale);
        if (!(value instanceof Array) || value.length === 0) {
            throw new Error(label + " is invalid");
        }
        if (value.length === 2 && typeof value[0] === "number" && typeof value[1] === "number") {
            pairs.push(value);
        } else {
            if (value.length !== dimensions) { throw new Error(label + " dimension count is invalid"); }
            for (index = 0; index < value.length; index += 1) {
                pair = value[index];
                if (!(pair instanceof Array) || pair.length !== 2 || typeof pair[0] !== "number" || typeof pair[1] !== "number") {
                    throw new Error(label + " is invalid");
                }
                pairs.push(pair);
            }
        }
        if (typeof KeyframeEase === "undefined") {
            throw new Error("temporal easing is unavailable");
        }
        var eases = [];
        for (index = 0; index < pairs.length; index += 1) {
            speed = pairs[index][0] * speedScale;
            influence = pairs[index][1];
            if (!isFinite(speed) || Math.abs(speed) > 1000000 || !isFinite(influence) || influence < 0.1 || influence > 100) {
                throw new Error(label + " is invalid");
            }
            eases.push(new KeyframeEase(speed, influence));
        }
        if (eases.length === 1 && dimensions > 1) {
            for (index = 1; index < dimensions; index += 1) { eases.push(eases[0]); }
        }
        return eases;
    }

    function applyTemporalEase(property, keyIndex, keyframe, speedScale) {
        var fallbackIn;
        var fallbackOut;
        var inEase;
        var outEase;
        var dimensions = propertyDimensionCount(property);
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
        if (fallbackIn instanceof Array && fallbackIn.length > 0 && fallbackIn.length !== dimensions) { throw new Error("temporal easing dimension count is invalid"); }
        if (fallbackOut instanceof Array && fallbackOut.length > 0 && fallbackOut.length !== dimensions) { throw new Error("temporal easing dimension count is invalid"); }
        inEase = keyframe.ease_in === undefined ? null : temporalEase(keyframe.ease_in, "ease_in", dimensions, speedScale);
        outEase = keyframe.ease_out === undefined ? null : temporalEase(keyframe.ease_out, "ease_out", dimensions, speedScale);
        for (index = 0; index < dimensions; index += 1) {
            if (inEase) {
                inEases.push(inEase[index]);
            } else if (fallbackIn instanceof Array && fallbackIn[index]) {
                inEases.push(fallbackIn[index]);
            } else {
                throw new Error("temporal easing is unavailable");
            }
            if (outEase) {
                outEases.push(outEase[index]);
            } else if (fallbackOut instanceof Array && fallbackOut[index]) {
                outEases.push(fallbackOut[index]);
            } else {
                throw new Error("temporal easing is unavailable");
            }
        }
        if (inEases.length !== dimensions || outEases.length !== dimensions) {
            throw new Error("temporal easing dimension count is invalid");
        }
        property.setTemporalEaseAtKey(keyIndex, inEases, outEases);
    }

    function setTimedProperty(property, value, frame, time, keyframe, speedScale) {
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
            applyTemporalEase(property, keyIndex, keyframe, speedScale);
        }
    }

    function setSeparatedPositionAtFrame(layer, axis, value, frame, time, keyframe) {
        var position = layer.transform.position;
        var property;
        position.dimensionsSeparated = true;
        property = axis === "x" ? layer.transform.xPosition : layer.transform.yPosition;
        if (!property) { throw new Error("separated position is unavailable"); }
        setTimedProperty(property, value, frame, time, keyframe);
    }

    function mappedTextAnchor(layer, value) {
        var rect;
        if (typeof TextLayer === "undefined" || !(layer instanceof TextLayer)) {
            return value;
        }
        rect = layer.sourceRectAtTime(0, false);
        return [
            Number(value[0]) + Number(rect.left),
            Number(value[1]) + Number(rect.top)
        ];
    }

    function setPropertyAtFrame(layer, propertyName, value, frame, time, keyframe, speedScale) {
        var property;
        switch (propertyName) {
            case "ADBE Position":
                property = layer.transform.position;
                break;
            case "ADBE Scale":
                property = layer.transform.scale;
                break;
            case "ADBE Anchor Point":
                property = layer.transform.anchorPoint;
                break;
            case "ADBE Rotate Z":
                property = layer.transform.zRotation;
                break;
            case "ADBE Rotate X":
                property = layer.transform.xRotation;
                break;
            case "ADBE Rotate Y":
                property = layer.transform.yRotation;
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
            case "ADBE Linear Wipe-0001":
            case "ADBE Linear Wipe-0002":
            case "ADBE Linear Wipe-0003":
                var wipe = layer.property("ADBE Effect Parade").property("ADBE Linear Wipe");
                if (!wipe) { throw new Error("Linear Wipe is not installed on the layer"); }
                property = wipe.property(propertyName);
                break;
            default:
                throw new Error("property is not in the fixed catalog");
        }
        if (propertyName === "ADBE Anchor Point") {
            value = mappedTextAnchor(layer, value);
        }
        if (layer.threeDLayer && value instanceof Array && value.length === 2 && /^(ADBE Position|ADBE Scale|ADBE Anchor Point)$/.test(propertyName)) {
            var atTime = propertyTime(frame, time);
            var current = atTime === null ? property.value : property.valueAtTime(atTime, false);
            value = [value[0], value[1], current[2]];
            if (keyframe) {
                var expanded = {};
                var key;
                for (key in keyframe) { if (hasOwn(keyframe, key)) { expanded[key] = keyframe[key]; } }
                var easeNames = ["ease_in", "ease_out"];
                var easeIndex;
                for (easeIndex = 0; easeIndex < easeNames.length; easeIndex += 1) {
                    var easeName = easeNames[easeIndex];
                    var ease = keyframe[easeName];
                    if (ease instanceof Array) {
                        expanded[easeName] = ease[0] instanceof Array
                            ? [ease[0], ease[1], [0, 33.333333]]
                            : [ease, ease, [0, 33.333333]];
                    }
                }
                keyframe = expanded;
            }
        }
        setTimedProperty(property, value, frame, time, keyframe, speedScale);
    }

    function setScaleComponentAtFrame(layer, component, value, frame, time, keyframe) {
        var property = layer.transform.scale;
        var atTime = propertyTime(frame, time);
        var current = atTime === null ? property.value : property.valueAtTime(atTime, false);
        current[component] = Number(value);
        setTimedProperty(property, current, frame, time, keyframe);
    }

    function requireTextLayer(layer) {
        if (typeof TextLayer === "undefined" || !(layer instanceof TextLayer)) {
            throw new Error("text operation requires TextLayer");
        }
        return layer;
    }

    function setText(layer, text, fontName, fontSize, color) {
        layer = requireTextLayer(layer);
        var textProperty = layer.property("ADBE Text Properties").property("ADBE Text Document");
        var document = textProperty.value;
        document.text = String(text);
        if (fontName !== undefined && fontName !== null) { document.font = String(fontName); }
        if (fontSize !== undefined && fontSize !== null) { document.fontSize = Number(fontSize); }
        if (color !== undefined && color !== null) { document.fillColor = normalizedColor(color).slice(0, 3); }
        textProperty.setValue(document);
    }

    function normalizedColor(value) {
        var channels = [];
        var index;
        assertColor(value, "color");
        if (typeof value === "string") {
            channels.push(parseInt(value.substring(1, 3), 16) / 255);
            channels.push(parseInt(value.substring(3, 5), 16) / 255);
            channels.push(parseInt(value.substring(5, 7), 16) / 255);
            return channels;
        }
        for (index = 0; index < value.length; index += 1) {
            channels.push(value[index]);
        }
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
        layer = requireTextLayer(layer);
        var textProperty = layer.property("ADBE Text Properties").property("ADBE Text Document");
        var document = textProperty.value;
        document.font = String(fontName);
        textProperty.setValue(document);
    }

    function setEffect(layer, effectName, properties) {
        var effect;
        switch (effectName) {
            case "ADBE Linear Wipe":
                effect = layer.property("ADBE Effect Parade").property(effectName);
                if (!effect) { effect = layer.property("ADBE Effect Parade").addProperty(effectName); }
                var wipeProperty;
                for (wipeProperty in properties) {
                    if (hasOwn(properties, wipeProperty)) { effect.property(wipeProperty).setValue(Number(properties[wipeProperty])); }
                }
                break;
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
            } else if (propertyName === "ADBE Position X") {
                setSeparatedPositionAtFrame(layer, "x", keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Position Y") {
                setSeparatedPositionAtFrame(layer, "y", keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Scale X") {
                setScaleComponentAtFrame(layer, 0, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Scale Y") {
                setScaleComponentAtFrame(layer, 1, keyframe.value, keyframe.frame, keyframe.time, keyframe);
            } else if (propertyName === "ADBE Opacity") {
                setPropertyAtFrame(layer, propertyName, Number(keyframe.value) * 100, keyframe.frame, keyframe.time, keyframe, 100);
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
                } else if (operation.property_name === "ADBE Position X") {
                    setSeparatedPositionAtFrame(layer, "x", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "ADBE Position Y") {
                    setSeparatedPositionAtFrame(layer, "y", operation.value, operation.frame, operation.time);
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
                } else if (operation.property_name === "position_x") {
                    setSeparatedPositionAtFrame(layer, "x", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "position_y") {
                    setSeparatedPositionAtFrame(layer, "y", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale") {
                    setPropertyAtFrame(layer, "ADBE Scale", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale_x") {
                    setScaleComponentAtFrame(layer, 0, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "scale_y") {
                    setScaleComponentAtFrame(layer, 1, operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "anchor") {
                    setPropertyAtFrame(layer, "ADBE Anchor Point", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "rotation") {
                    setPropertyAtFrame(layer, "ADBE Rotate Z", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "rotation_x") {
                    setPropertyAtFrame(layer, "ADBE Rotate X", operation.value, operation.frame, operation.time);
                } else if (operation.property_name === "rotation_y") {
                    setPropertyAtFrame(layer, "ADBE Rotate Y", operation.value, operation.frame, operation.time);
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
                setText(layer, operation.text, operation.font_name, operation.font_size, operation.color);
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

    function findAssetItem(assetId) {
        var marker = "keepframe:asset=" + assetId;
        var found = null;
        var index;
        var item;
        if (!app.project) { return null; }
        for (index = 1; index <= app.project.numItems; index += 1) {
            item = app.project.item(index);
            if (String(item.comment || "") !== marker) { continue; }
            if (found) { throw new Error("duplicate ambiguous server asset"); }
            found = item;
        }
        return found;
    }

    function relinkSessionAssets() {
        var prefix = "keepframe:asset=";
        var index;
        var item;
        var assetId;
        var file;
        if (!app.project || MEDIA_DIR === null) {
            throw new Error("session media directory is unavailable");
        }
        for (index = 1; index <= app.project.numItems; index += 1) {
            item = app.project.item(index);
            if (item instanceof CompItem || !item.mainSource) {
                continue;
            }
            if (String(item.comment || "").indexOf(prefix) !== 0) {
                if (item.file && item.usedIn && item.usedIn.length > 0) {
                    throw new Error("used project footage is outside the approved asset contract");
                }
                continue;
            }
            assetId = String(item.comment).substring(prefix.length);
            if (!ASSET_ID_PATTERN.test(assetId)) {
                throw new Error("tagged server asset id is invalid");
            }
            file = File(MEDIA_DIR.fsName + "/" + assetId);
            if (!file.exists || file.length <= 0 || typeof item.replace !== "function") {
                throw new Error("session media is unavailable");
            }
            item.replace(file);
            if (String(item.comment || "") !== prefix + assetId) {
                item.comment = prefix + assetId;
            }
        }
    }

    function addMappedLayer(operation) {
        var comp = requireSessionComposition();
        var layer;
        var footage;
        switch (operation.layer_type) {
            case "null":
                layer = comp.layers.addNull();
                break;
            case "text":
                layer = comp.layers.addText(operation.name);
                break;
            case "solid":
                layer = comp.layers.addSolid(normalizedColor(operation.color).slice(0, 3), operation.name, operation.width, operation.height, 1);
                break;
            case "footage":
            case "model":
                if (operation.layer_type === "model") {
                    if (!modelLayersAvailable()) { throw new Error("model_layers capability is unavailable"); }
                    comp.renderer = advanced3DRenderer(comp);
                }
                footage = findAssetItem(operation.asset_id);
                if (!footage || footage instanceof CompItem || !footage.mainSource) {
                    throw new Error("server asset is not imported");
                }
                layer = comp.layers.add(footage);
                if (operation.layer_type === "model" && !layer.threeDLayer) { throw new Error("GLB did not create a 3D model layer"); }
                break;
            default:
                throw new Error("layer type is not in the fixed catalog");
        }
        layer.comment = "keepframe:layer=" + operation.layer_instance_id + ";source_element_id=" + (operation.source_element_id || "");
        layer.name = operation.name;
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
    function assertSha256(value, label) {
        if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
            throw new Error(label + " must be a lowercase SHA-256 digest");
        }
    }
    function assertIdentifier(value, label) {
        if (typeof value !== "string" || !SESSION_ID_PATTERN.test(value)) {
            throw new Error(label + " must be a safe identifier");
        }
    }

    function assertNativeLayerId(value, label) {
        if (
            typeof value !== "number"
            || !isFinite(value)
            || value < 1
            || value > 1000000
            || Math.floor(value) !== value
        ) {
            throw new Error(label + " must be a positive native layer id");
        }
    }
    function assertAssetIdentifier(value, label) {
        if (typeof value !== "string" || !ASSET_ID_PATTERN.test(value)) {
            throw new Error(label + " must be a safe asset identifier");
        }
    }


    function assertSolidDimension(value, label) {
        if (typeof value !== "number" || !isFinite(value) || value < 4 || value > 30000 || Math.floor(value) !== value) {
            throw new Error(label + " is outside bounds");
        }
    }

    function assertColor(value, label) {
        var index;
        if (typeof value === "string") {
            if (!/^#[0-9a-fA-F]{6}$/.test(value)) {
                throw new Error(label + " must be an RGB color in #RRGGBB form");
            }
            return;
        }
        if (!(value instanceof Array) || value.length !== 3) {
            throw new Error(label + " must contain exactly three RGB channels");
        }
        for (index = 0; index < value.length; index += 1) {
            if (typeof value[index] !== "number" || !isFinite(value[index]) || value[index] < 0 || value[index] > 1) {
                throw new Error(label + " channel is invalid");
            }
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
        var pair;
        var index;
        if (!(value instanceof Array) || value.length === 0) {
            throw new Error(label + " is invalid");
        }
        if (value.length === 2 && typeof value[0] === "number" && typeof value[1] === "number") {
            pair = value;
            if (!isFinite(pair[0]) || Math.abs(pair[0]) > 1000000 || !isFinite(pair[1]) || pair[1] < 0.1 || pair[1] > 100) {
                throw new Error(label + " is invalid");
            }
            return;
        }
        for (index = 0; index < value.length; index += 1) {
            pair = value[index];
            if (!(pair instanceof Array) || pair.length !== 2 || typeof pair[0] !== "number" || typeof pair[1] !== "number") {
                throw new Error(label + " is invalid");
            }
            if (!isFinite(pair[0]) || Math.abs(pair[0]) > 1000000 || !isFinite(pair[1]) || pair[1] < 0.1 || pair[1] > 100) {
                throw new Error(label + " is invalid");
            }
        }
    }

    function assertTemporalEaseDimensions(value, label, dimensions) {
        if (value instanceof Array && value.length > 0 && value[0] instanceof Array && value.length !== dimensions) {
            throw new Error(label + " dimension count is invalid");
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
        if (/^ADBE Linear Wipe-000[123]$/.test(propertyName)) { requiredEffect = "ADBE Linear Wipe"; }
        if (requiredEffect !== undefined && !catalogName(catalog, "effects", requiredEffect)) {
            throw new Error("property requires an approved effect");
        }
        if (propertyName === "ADBE Opacity" && (typeof value !== "number" || !isFinite(value) || value < 0 || value > 1)) {
            throw new Error("opacity is outside the unit interval");
        }
        if (propertyName === "ADBE Linear Wipe-0001" && (typeof value !== "number" || !isFinite(value) || value < 0 || value > 100)) { throw new Error("Linear Wipe completion is outside bounds"); }
        schema = String(catalog.properties[propertyName]).toLowerCase();
        if (schema === "number" || schema === "float") {
            if (typeof value !== "number" || !isFinite(value)) { throw new Error("property value type is invalid"); }
        } else if (schema === "boolean" || schema === "bool") {
            if (typeof value !== "boolean") { throw new Error("property value type is invalid"); }
        } else if (schema === "string" || schema === "str" || schema === "enum") {
            assertString(value, "property value");
        } else if (schema === "color" || schema === "rgba") {
            assertColor(value, "property color");
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
        if (/^ADBE Linear Wipe-000[123]$/.test(propertyName) && !/^(set_effect|add_effect|set_layer_effect|set_keyframes|set_property_keyframes)$/.test(kind)) {
            throw new Error("Linear Wipe properties require set_effect or set_keyframes");
        }
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
        if (propertyName !== "ADBE Fill Color" && !/^ADBE Linear Wipe-000[123]$/.test(propertyName) && !hasOwn(fixedPropertySchemas(), propertyName)) {
            throw new Error("property is not in the fixed catalog");
        }
    }

    function validateOperation(payload, operation, frameLimit) {
        assertObject(operation, "operation");
        assertString(operation.kind, "operation kind");
        assertIdentifier(operation.layer_instance_id, "layer_instance_id");
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
                if (!/^(position|position_x|position_y|scale|scale_x|scale_y|rotation|rotation_x|rotation_y|skew|skew_x|skew_y|anchor)$/.test(operation.property_name)) { throw new Error("transform property is invalid"); }
                assertSafeValue(operation.value, 0);
                capabilityProperty(payload, {
                    position: "ADBE Position",
                    position_x: "ADBE Position X",
                    position_y: "ADBE Position Y",
                    scale: "ADBE Scale",
                    scale_x: "ADBE Scale X",
                    scale_y: "ADBE Scale Y",
                    rotation: "ADBE Rotate Z",
                    rotation_x: "ADBE Rotate X",
                    rotation_y: "ADBE Rotate Y",
                    skew: "ADBE Skew",
                    skew_x: "ADBE Skew",
                    skew_y: "ADBE Skew Axis",
                    anchor: "ADBE Anchor Point"
                }[operation.property_name], operation.value);
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
                var keyframeDimensions = /^(ADBE Position|ADBE Scale|ADBE Anchor Point)$/.test(operation.property_name) ? 2 : 1;
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
                    if (keyframe.ease_in !== undefined) { assertTemporalEaseDimensions(keyframe.ease_in, "ease_in", keyframeDimensions); }
                    if (keyframe.ease_out !== undefined) { assertTemporalEaseDimensions(keyframe.ease_out, "ease_out", keyframeDimensions); }
                    capabilityProperty(payload, operation.property_name, keyframe.value);
                }
                break;
            case "set_text":
            case "set_layer_text":
                assertKeys(operation, { kind: true, layer_instance_id: true, text: true, font_name: true, font_size: true, color: true }, "set_text");
                assertString(operation.text, "text");
                if (operation.font_name !== undefined && operation.font_name !== null && !catalogName(payload.approved_capabilities, "fonts", operation.font_name)) { throw new Error("font is not in the approved catalog"); }
                if (operation.font_size !== undefined && operation.font_size !== null && (typeof operation.font_size !== "number" || !isFinite(operation.font_size) || operation.font_size <= 0 || operation.font_size > 1000000)) { throw new Error("font size is invalid"); }
                if (operation.color !== undefined && operation.color !== null) { assertColor(operation.color, "text color"); }
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
                assertKeys(operation, { kind: true, layer_instance_id: true, layer_type: true, name: true, source_element_id: true, parent_instance_id: true, asset_id: true, width: true, height: true, color: true }, "add_layer");
                if (!/^(text|solid|null|footage|model)$/.test(operation.layer_type)) { throw new Error("layer type is invalid"); }
                if (operation.layer_type === "model" && (payload.approved_capabilities.model_layers !== true || !modelLayersAvailable())) { throw new Error("model_layers capability is unavailable"); }
                assertString(operation.name, "layer name");
                if (operation.name.length === 0) { throw new Error("layer name must not be empty"); }
                if (operation.source_element_id !== undefined && operation.source_element_id !== null) { assertIdentifier(operation.source_element_id, "source_element_id"); }
                if (operation.parent_instance_id !== undefined && operation.parent_instance_id !== null) { assertIdentifier(operation.parent_instance_id, "parent_instance_id"); }
                if (operation.layer_type === "footage" || operation.layer_type === "model") {
                    if (operation.asset_id === undefined || operation.asset_id === null) { throw new Error("footage layers require asset_id"); }
                    assertAssetIdentifier(operation.asset_id, "asset_id");
                    if (operation.width !== undefined && operation.width !== null || operation.height !== undefined && operation.height !== null || operation.color !== undefined && operation.color !== null) { throw new Error("footage layers forbid solid fields"); }
                } else if (operation.layer_type === "solid") {
                    if (operation.asset_id !== undefined && operation.asset_id !== null) { throw new Error("solid layers forbid asset_id"); }
                    if (operation.width === undefined || operation.width === null || operation.height === undefined || operation.height === null || operation.color === undefined || operation.color === null) { throw new Error("solid layers require width, height, and color"); }
                    assertSolidDimension(operation.width, "solid width");
                    assertSolidDimension(operation.height, "solid height");
                    assertColor(operation.color, "solid color");
                } else if (operation.asset_id !== undefined && operation.asset_id !== null || operation.width !== undefined && operation.width !== null || operation.height !== undefined && operation.height !== null || operation.color !== undefined && operation.color !== null) {
                    throw new Error("text and null layers forbid footage/solid fields");
                }
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
        function checkFrame(value, allowEnd) {
            if (value === undefined || value === null) { return; }
            if (typeof value !== "number" || !isFinite(value) || value < 0 || Math.floor(value) !== value) { throw new Error("operation frame is invalid"); }
            if (allowEnd ? value > frameLimit : value >= frameLimit) { throw new Error("operation frame is outside the scene"); }
        }
        function checkTime(value) {
            if (value === undefined || value === null) { return; }
            if (typeof value !== "number" || !isFinite(value) || value < 0) { throw new Error("operation time is invalid"); }
            if (value > duration) { throw new Error("operation time is outside the comp"); }
        }
        frame = operation.frame;
        checkFrame(frame);
        checkFrame(operation.frame_start);
        checkFrame(operation.frame_end, true);
        if (operation.frame_start !== undefined && operation.frame_start !== null && operation.frame_end !== undefined && operation.frame_end !== null && operation.frame_end < operation.frame_start) {
            throw new Error("visibility frame range is reversed");
        }
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
        var approved_capabilities;
        assertObject(payload, "apply_operation_batch payload");
        assertKeys(payload, {
            batch: true,
            approved_capabilities: true,
            scene_frame_count: true,
            duration: true,
            layer_count: true,
            baseline: true,
            locked_source_ids: true,
            layer_sources: true,
            layer_native_ids: true,
            project_id: true,
            plan_id: true,
            session_id: true
        }, "apply_operation_batch payload");
        assertObject(payload.batch, "batch");
        assertKeys(payload.batch, { operations: true, capability_digest: true, scene_frame_count: true, layer_count: true, duration: true }, "batch");
        assertObject(payload.approved_capabilities, "approved_capabilities");
        approved_capabilities = payload.approved_capabilities;
        assertSha256(approved_capabilities.digest, "approved capability digest");
        if (typeof payload.baseline !== "boolean") { throw new Error("baseline must be boolean"); }
        batch = payload.batch;
        assertSha256(batch.capability_digest, "batch capability digest");
        if (!(batch.capability_digest === approved_capabilities.digest)) {
            throw new Error("operation batch capability digest does not match the approved catalog");
        }
        operations = batch.operations;
        if (!(operations instanceof Array) || operations.length > 128) { throw new Error("operation batch is outside bounds"); }
        if (JSON.stringify(payload).length > MAX_JSON_BYTES) { throw new Error("operation batch exceeds 1 MiB"); }
        for (index = 0; index < operations.length; index += 1) {
            validateOperation(payload, operations[index], frameLimit);
            validateTiming(operations[index], frameLimit, duration);
        }
        return batch;
    }

    function mappedLayerInventory() {
        var inventory = {};
        var comp = requireSessionComposition();
        var index;
        var layer;
        var metadata;
        for (index = 1; index <= comp.numLayers; index += 1) {
            layer = comp.layer(index);
            metadata = layerMetadata(layer);
            if (!metadata) { continue; }
            if (hasOwn(inventory, metadata.layer_instance_id)) {
                throw new Error("duplicate mapped layer instance id");
            }
            inventory[metadata.layer_instance_id] = metadata.source_element_id;
        }
        return inventory;
    }

    function mappedLayerNativeInventory() {
        var inventory = {};
        var nativeOwners = {};
        var comp = requireSessionComposition();
        var index;
        var layer;
        var metadata;
        var nativeId;
        for (index = 1; index <= comp.numLayers; index += 1) {
            layer = comp.layer(index);
            metadata = layerMetadata(layer);
            if (!metadata) { continue; }
            nativeId = nativeLayerId(layer);
            if (hasOwn(inventory, metadata.layer_instance_id)) {
                throw new Error("duplicate mapped layer instance id");
            }
            if (hasOwn(nativeOwners, String(nativeId))) {
                throw new Error("duplicate native layer id");
            }
            inventory[metadata.layer_instance_id] = nativeId;
            nativeOwners[String(nativeId)] = metadata.layer_instance_id;
        }
        return inventory;
    }
    function inventorySize(inventory) {
        var count = 0;
        var key;
        for (key in inventory) {
            if (hasOwn(inventory, key)) { count += 1; }
        }
        return count;
    }

    function validateApplyContext(payload) {
        var actual = mappedLayerInventory();
        var actualNative = mappedLayerNativeInventory();
        var inventory = {};
        var nativeInventory = {};
        var nativeOwners = {};
        var locked = {};
        var sourceIds = payload.locked_source_ids;
        var key;
        var source;
        var nativeId;
        var index;
        if (typeof payload.baseline !== "boolean") { throw new Error("baseline must be boolean"); }
        if (!(sourceIds instanceof Array)) { throw new Error("locked_source_ids must be a list"); }
        for (index = 0; index < sourceIds.length; index += 1) {
            assertIdentifier(sourceIds[index], "locked source id");
            if (hasOwn(locked, sourceIds[index])) { throw new Error("locked_source_ids contains duplicate ids"); }
            locked[sourceIds[index]] = true;
        }
        assertObject(payload.layer_sources, "layer_sources");
        for (key in payload.layer_sources) {
            if (!hasOwn(payload.layer_sources, key)) { continue; }
            assertIdentifier(key, "layer instance id");
            source = payload.layer_sources[key];
            if (source !== null) { assertIdentifier(source, "layer source id"); }
            inventory[key] = source;
        }
        assertObject(payload.layer_native_ids, "layer_native_ids");
        if (inventorySize(payload.layer_native_ids) > 1000) {
            throw new Error("native layer inventory exceeds 1000 layers");
        }
        for (key in payload.layer_native_ids) {
            if (!hasOwn(payload.layer_native_ids, key)) { continue; }
            assertIdentifier(key, "native layer instance id");
            nativeId = payload.layer_native_ids[key];
            assertNativeLayerId(nativeId, "native layer id");
            if (hasOwn(nativeOwners, String(nativeId))) {
                throw new Error("duplicate native layer id");
            }
            nativeOwners[String(nativeId)] = key;
            nativeInventory[key] = nativeId;
        }
        for (key in actual) {
            if (!hasOwn(actual, key) || !hasOwn(inventory, key) || actual[key] !== inventory[key]) {
                throw new Error("mapped layer inventory does not match the bound composition");
            }
            if (!hasOwn(actualNative, key) || !hasOwn(nativeInventory, key)) {
                throw new Error("native layer inventory does not match the bound composition");
            }
            if (actualNative[key] !== nativeInventory[key]) {
                throw new Error("native layer id does not match the bound composition");
            }
        }
        for (key in inventory) {
            if (hasOwn(inventory, key) && !hasOwn(actual, key)) {
                throw new Error("layer inventory contains an unknown mapped layer");
            }
        }
        for (key in nativeInventory) {
            if (hasOwn(nativeInventory, key) && !hasOwn(actualNative, key)) {
                throw new Error("native layer inventory contains an unknown mapped layer");
            }
        }
        return {
            baseline: payload.baseline,
            locked: locked,
            inventory: inventory,
            native_inventory: nativeInventory
        };
    }

    function authorizeOperations(payload, operations) {
        var context = validateApplyContext(payload);
        var inventory = context.inventory;
        var locked = context.locked;
        var added = {};
        var removed = {};
        var operation;
        var instanceId;
        var source;
        var index;
        function requireTarget(targetId) {
            if (!hasOwn(inventory, targetId)) { throw new Error("layer instance is not in the inventory"); }
            source = inventory[targetId];
            if (!context.baseline && source !== null && hasOwn(locked, source)) {
                throw new Error("layer source is locked");
            }
        }
        for (index = 0; index < operations.length; index += 1) {
            operation = operations[index];
            instanceId = operation.layer_instance_id;
            if (operation.kind === "add_layer") {
                if (hasOwn(inventory, instanceId) || hasOwn(added, instanceId) || hasOwn(removed, instanceId)) {
                    throw new Error("add_layer reuses layer instance id");
                }
                if (operation.parent_instance_id !== undefined && operation.parent_instance_id !== null) {
                    requireTarget(operation.parent_instance_id);
                }
                if (!context.baseline) {
                    if (operation.source_element_id !== undefined && operation.source_element_id !== null) {
                        throw new Error("non-baseline add_layer must be source-less");
                    }
                    if (!/^agent[-_:][A-Za-z0-9_.:-]{1,255}$/.test(instanceId)) {
                        throw new Error("non-baseline add_layer requires an agent layer id");
                    }
                }
                inventory[instanceId] = operation.source_element_id === undefined ? null : operation.source_element_id;
                if (inventorySize(inventory) > 1000) {
                    throw new Error("final layer inventory exceeds 1000 layers");
                }
                added[instanceId] = true;
            } else {
                requireTarget(instanceId);
                if (operation.kind === "remove_layer") {
                    delete inventory[instanceId];
                    removed[instanceId] = true;
                }
            }
        }
        if (inventorySize(inventory) > 1000) {
            throw new Error("final layer inventory exceeds 1000 layers");
        }
        return context;
    }

    function preflightBatch(operations) {
        var kinds = {};
        var comp = requireSessionComposition();
        var layer;
        var metadata;
        var footage;
        var operation;
        var index;
        for (index = 1; index <= comp.numLayers; index += 1) {
            layer = comp.layer(index);
            metadata = layerMetadata(layer);
            if (!metadata) { continue; }
            kinds[metadata.layer_instance_id] = (
                typeof TextLayer !== "undefined" && layer instanceof TextLayer
            ) ? "text" : "other";
        }
        for (index = 0; index < operations.length; index += 1) {
            operation = operations[index];
            if (operation.kind === "add_layer") {
                if (operation.layer_type === "footage" || operation.layer_type === "model") {
                    if (operation.layer_type === "model") { advanced3DRenderer(comp); }
                    footage = findAssetItem(operation.asset_id);
                    if (!footage || footage instanceof CompItem || !footage.mainSource) {
                        throw new Error("server asset is not imported");
                    }
                }
                kinds[operation.layer_instance_id] = operation.layer_type;
                continue;
            }
            if (!hasOwn(kinds, operation.layer_instance_id)) {
                throw new Error("layer is not mapped");
            }
            if (operation.kind === "set_text" || operation.kind === "set_layer_text" || operation.kind === "set_font" || operation.kind === "set_layer_font") {
                if (kinds[operation.layer_instance_id] !== "text") {
                    throw new Error("text operation requires TextLayer");
                }
            }
            if (operation.kind === "remove_layer") {
                delete kinds[operation.layer_instance_id];
            }
        }
    }

    function captureBatchSnapshot() {
        var comp = requireSessionComposition();
        return {
            layer_count: comp.numLayers,
            inventory: mappedLayerInventory(),
            layer_native_ids: mappedLayerNativeInventory()
        };
    }

    function sameBatchInventory(expected, actual) {
        var key;
        if (inventorySize(expected) !== inventorySize(actual)) { return false; }
        for (key in expected) {
            if (hasOwn(expected, key) && (!hasOwn(actual, key) || expected[key] !== actual[key])) {
                return false;
            }
        }
        return true;
    }

    function verifyBatchSnapshot(snapshot) {
        var comp = requireSessionComposition();
        var actual = mappedLayerInventory();
        var actualNative = mappedLayerNativeInventory();
        if (
            comp.numLayers !== snapshot.layer_count
            || !sameBatchInventory(snapshot.inventory, actual)
            || !sameBatchInventory(snapshot.layer_native_ids, actualNative)
        ) {
            throw new Error("layer inventory/source/native mapping does not match pre-batch snapshot");
        }
    }

    function batchErrorText(error) {
        if (error && error.message !== undefined) { return String(error.message); }
        return String(error);
    }

    function applyBatch(payload) {
        var comp;
        var sceneFrameCount;
        var duration;
        var layerCount;
        var batch;
        var operations;
        var snapshot;
        var index;
        var groupStarted = false;
        var applyFailed = false;
        var applyError = null;
        var stopped = false;
        var rollbackFailed;
        var rollbackError;
        assertObject(payload, "apply_operation_batch payload");
        comp = requireSessionComposition();
        sceneFrameCount = payload.scene_frame_count;
        duration = payload.duration;
        layerCount = payload.layer_count;
        validateAuthoritativeBounds(comp, sceneFrameCount, duration, layerCount);
        batch = validatedBatch(payload, sceneFrameCount, duration);
        operations = batch.operations;
        authorizeOperations(payload, operations);
        preflightBatch(operations);
        snapshot = captureBatchSnapshot();
        for (index = 0; index < operations.length; index += 1) {
            if (operations[index].kind === "add_layer") { layerCount += 1; }
            if (operations[index].kind === "remove_layer") { layerCount = Math.max(0, layerCount - 1); }
            if (layerCount > 1000) { throw new Error("layer count exceeds 1000"); }
        }
        activeBatch = true;
        try {
            try {
                app.beginUndoGroup("Keepframe operation batch");
                groupStarted = true;
                for (index = 0; index < operations.length; index += 1) {
                    applyOperation(operations[index]);
                }
                stopped = STOP_FILE.exists;
            } catch (error) {
                applyFailed = true;
                applyError = error;
            } finally {
                if (groupStarted) {
                    try {
                        app.endUndoGroup();
                    } catch (endError) {
                        if (!applyFailed) {
                            applyFailed = true;
                            applyError = endError;
                        }
                    }
                }
            }
        } finally {
            activeBatch = false;
        }
        removeStop();
        if (applyFailed) {
            if (!groupStarted) {
                throw new Error("apply failed: " + batchErrorText(applyError) + "; rollback not required");
            }
            rollbackFailed = false;
            rollbackError = null;
            try {
                app.executeCommand(app.findMenuCommandId("Undo"));
                verifyBatchSnapshot(snapshot);
            } catch (error) {
                rollbackFailed = true;
                rollbackError = error;
            }
            if (rollbackFailed) {
                throw new Error("apply failed: " + batchErrorText(applyError) + "; rollback failed: " + batchErrorText(rollbackError));
            }
            throw new Error("apply failed: " + batchErrorText(applyError) + "; rollback succeeded");
        }
        return {
            applied: operations.length,
            stop_requested: stopped,
            layer_sources: mappedLayerInventory(),
            layer_native_ids: mappedLayerNativeInventory()
        };
    }

    function projectSettings(payload) {
        var required = ["width", "height", "frame_rate", "duration", "background_color"];
        var index;
        var color;
        for (index = 0; index < required.length; index += 1) {
            if (!hasOwn(payload, required[index])) { throw new Error("project setting is required: " + required[index]); }
        }
        assertSolidDimension(payload.width, "width");
        assertSolidDimension(payload.height, "height");
        if (typeof payload.frame_rate !== "number" || !isFinite(payload.frame_rate) || payload.frame_rate < 1 || payload.frame_rate > 99) {
            throw new Error("frame rate is invalid");
        }
        if (typeof payload.duration !== "number" || !isFinite(payload.duration) || payload.duration <= 0 || payload.duration > 10800) {
            throw new Error("duration is invalid");
        }
        assertColor(payload.background_color, "background color");
        color = normalizedColor(payload.background_color).slice(0, 3);
        return {
            width: payload.width,
            height: payload.height,
            frame_rate: payload.frame_rate,
            duration: payload.duration,
            background_color: color
        };
    }

    function projectSettingsMatch(comp, settings) {
        var color = comp.bgColor;
        var index;
        if (
            comp.width !== settings.width
            || comp.height !== settings.height
            || !approximatelyEqual(comp.frameRate, settings.frame_rate)
            || !approximatelyEqual(comp.duration, settings.duration)
            || !(color instanceof Array)
            || color.length < 3
        ) {
            return false;
        }
        for (index = 0; index < 3; index += 1) {
            if (!approximatelyEqual(Number(color[index]), settings.background_color[index])) { return false; }
        }
        return true;
    }

    function createOrOpenProject(payload) {
        var marker;
        var info;
        var settings;
        var comp = null;
        var checkpointIndex;
        var file;
        assertObject(payload, "create_or_open_project payload");
        assertKeys(payload, {
            project_id: true,
            plan_id: true,
            session_id: true,
            width: true,
            height: true,
            frame_rate: true,
            duration: true,
            background_color: true,
            checkpoint_index: true
        }, "create_or_open_project payload");
        marker = sessionMarker(payload.project_id, payload.plan_id, payload.session_id);
        bindSessionDirectories(payload.project_id, payload.plan_id, payload.session_id);
        info = heartbeat(false);
        settings = projectSettings(payload);
        if (!info.ready) { throw new Error("After Effects 2022 or newer is required"); }
        if (hasOwn(payload, "checkpoint_index")) {
            checkpointIndex = payload.checkpoint_index;
            if (
                typeof checkpointIndex !== "number"
                || !isFinite(checkpointIndex)
                || checkpointIndex < 0
                || checkpointIndex > 1000000
                || Math.floor(checkpointIndex) !== checkpointIndex
            ) {
                throw new Error("checkpoint index is invalid");
            }
            file = File(CHECKPOINT_DIR.fsName + "/checkpoint-" + String(checkpointIndex) + ".aep");
            if (!file.exists) { throw new Error("checkpoint file is unavailable"); }
            // A replacement connector may reuse this panel process.  Never
            // carry the prior device's in-memory composition into a selected
            // server checkpoint.
            SESSION_COMP = null;
            SESSION_MARKER = null;
            app.open(file);
            relinkSessionAssets();
            comp = findSessionComposition(marker);
            if (!comp || !projectSettingsMatch(comp, settings)) {
                throw new Error("checkpoint session composition does not match the requested project");
            }
            SESSION_MARKER = marker;
            SESSION_COMP = comp;
            comp.openInViewer();
            return {
                opened: true,
                project_open: true,
                checkpoint_index: checkpointIndex,
                heartbeat: heartbeat(false)
            };
        }
        if (SESSION_MARKER === marker && SESSION_COMP && compositionIsInProject(SESSION_COMP) && hasSessionMarker(SESSION_COMP, marker)) {
            comp = SESSION_COMP;
        } else if (app.project) {
            comp = findSessionComposition(marker);
        }
        if (comp) {
            if (!projectSettingsMatch(comp, settings)) {
                throw new Error("bound composition settings do not match the requested project");
            }
            SESSION_MARKER = marker;
            SESSION_COMP = comp;
            comp.openInViewer();
            return { opened: true, project_open: true, heartbeat: heartbeat(false) };
        }
        if (app.project) {
            throw new Error("refusing to reuse an unrelated After Effects project");
        }
        app.newProject();
        app.beginUndoGroup("Keepframe create session composition");
        try {
            comp = app.project.items.addComp("Keepframe", settings.width, settings.height, 1, settings.duration, settings.frame_rate);
            comp.bgColor = settings.background_color;
            comp.comment = marker;
            if (!hasSessionMarker(comp, marker) || !projectSettingsMatch(comp, settings)) {
                throw new Error("cannot establish Keepframe session composition");
            }
            SESSION_MARKER = marker;
            SESSION_COMP = comp;
        } finally {
            app.endUndoGroup();
        }
        comp.openInViewer();
        return { opened: true, project_open: true, heartbeat: heartbeat(false) };
    }

    function reopenImmutableCheckpoint(checkpointIndex) {
        var marker = SESSION_MARKER;
        var file;
        var comp;
        if (
            typeof checkpointIndex !== "number"
            || !isFinite(checkpointIndex)
            || checkpointIndex < 0
            || checkpointIndex > 1000000
            || Math.floor(checkpointIndex) !== checkpointIndex
        ) {
            throw new Error("checkpoint index is invalid");
        }
        if (typeof marker !== "string" || marker.length === 0) {
            throw new Error("session marker is unavailable");
        }
        file = File(CHECKPOINT_DIR.fsName + "/checkpoint-" + String(checkpointIndex) + ".aep");
        if (!file.exists || file.length <= 0) {
            throw new Error("checkpoint file is unavailable");
        }
        SESSION_COMP = null;
        SESSION_MARKER = null;
        if (app.project && typeof app.project.close === "function") {
            app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
        }
        app.open(file);
        relinkSessionAssets();
        comp = findSessionComposition(marker);
        if (!comp) {
            throw new Error("checkpoint session composition is unavailable");
        }
        SESSION_MARKER = marker;
        SESSION_COMP = comp;
        return comp;
    }

    function importServerAsset(payload) {
        requireSessionComposition();
        var assetId = payload.asset_id;
        var file;
        var footage;
        if (typeof assetId !== "string" || !ASSET_ID_PATTERN.test(assetId)) { throw new Error("asset id is invalid"); }
        if (/\.glb$/i.test(assetId) && !modelLayersAvailable()) { throw new Error("GLB import requires the model_layers capability"); }
        footage = findAssetItem(assetId);
        if (footage) {
            if (footage instanceof CompItem || !footage.mainSource) { throw new Error("server asset is not footage"); }
            return { imported: true, asset_id: assetId, item_name: footage.name };
        }
        file = File(MEDIA_DIR.fsName + "/" + assetId);
        if (!file.exists) { throw new Error("server-issued asset is unavailable"); }
        app.beginUndoGroup("Keepframe import server asset");
        try {
            footage = app.project.importFile(new ImportOptions(file));
            if (!footage || footage instanceof CompItem || !footage.mainSource) { throw new Error("server asset is not footage"); }
            footage.comment = "keepframe:asset=" + assetId;
            if (!findAssetItem(assetId)) { throw new Error("cannot tag imported server asset"); }
        } finally {
            app.endUndoGroup();
        }
        return { imported: true, asset_id: assetId, item_name: footage.name };
    }


    function validateInspectRequest(payload, comp) {
        var frameCount;
        var fps;
        var expectedFrameCount;
        var requested;
        var layerSources;
        var layerNativeIds;
        var manualIds;
        var seenPairs = {};
        var seenSources = {};
        var seenManual = {};
        var seenNative = {};
        var key;
        var pair;
        var source;
        var frame;
        var index;
        assertObject(payload, "inspect_mapped_layers payload");
        assertKeys(payload, {
            project_id: true,
            plan_id: true,
            session_id: true,
            frame_count: true,
            fps: true,
            capability_hash: true,
            requested: true,
            layer_sources: true,
            layer_native_ids: true,
            allow_new_agent_ids: true,
            checkpoint_index: true
        }, "inspect_mapped_layers payload");
        frameCount = payload.frame_count;
        fps = payload.fps;
        if (
            typeof frameCount !== "number"
            || !isFinite(frameCount)
            || frameCount < 1
            || frameCount > MAX_KEYFRAMES
            || Math.floor(frameCount) !== frameCount
        ) {
            throw new Error("inspection frame count is invalid");
        }
        if (typeof fps !== "number" || !isFinite(fps) || fps < 1 || fps > 99) {
            throw new Error("inspection fps is invalid");
        }
        assertSha256(payload.capability_hash, "capability hash");
        if (!approximatelyEqual(fps, comp.frameRate)) {
            throw new Error("inspection fps does not match the bound composition");
        }
        expectedFrameCount = comp.duration * comp.frameRate;
        if (!isFinite(expectedFrameCount) || !approximatelyEqual(frameCount, expectedFrameCount)) {
            throw new Error("inspection frame count does not match the bound composition");
        }
        if (JSON.stringify(payload).length > MAX_JSON_BYTES) {
            throw new Error("inspection request exceeds 1 MiB");
        }
        requested = payload.requested;
        if (!(requested instanceof Array) || requested.length > 4096) {
            throw new Error("inspection request pairs are outside bounds");
        }
        layerSources = payload.layer_sources;
        assertObject(layerSources, "layer_sources");
        if (inventorySize(layerSources) > 1000) {
            throw new Error("layer source inventory exceeds 1000 layers");
        }
        for (key in layerSources) {
            if (!hasOwn(layerSources, key)) { continue; }
            assertIdentifier(key, "layer instance id");
            source = layerSources[key];
            if (source !== null) { assertIdentifier(source, "layer source id"); }
        }
        layerNativeIds = payload.layer_native_ids;
        assertObject(layerNativeIds, "layer_native_ids");
        if (inventorySize(layerNativeIds) > 1000) {
            throw new Error("native layer inventory exceeds 1000 layers");
        }
        for (key in layerNativeIds) {
            if (!hasOwn(layerNativeIds, key)) { continue; }
            assertIdentifier(key, "native layer instance id");
            assertNativeLayerId(layerNativeIds[key], "native layer id");
            if (hasOwn(seenNative, String(layerNativeIds[key]))) {
                throw new Error("duplicate native layer id");
            }
            seenNative[String(layerNativeIds[key])] = key;
        }
        manualIds = [];
        if (hasOwn(payload, "allow_new_agent_ids")) {
            if (!(payload.allow_new_agent_ids instanceof Array) || payload.allow_new_agent_ids.length > 1000) {
                throw new Error("allow_new_agent_ids pool is outside bounds");
            }
            for (index = 0; index < payload.allow_new_agent_ids.length; index += 1) {
                source = payload.allow_new_agent_ids[index];
                assertIdentifier(source, "manual agent id");
                if (!/^agent[-_:][A-Za-z0-9_.:-]{1,255}$/.test(source)) {
                    throw new Error("manual agent id is invalid");
                }
                if (hasOwn(seenManual, source)) { throw new Error("duplicate manual agent id"); }
                if (hasOwn(layerSources, source)) { throw new Error("manual agent id is already in the inventory"); }
                seenManual[source] = true;
                manualIds.push(source);
            }
        }
        for (index = 0; index < requested.length; index += 1) {
            pair = requested[index];
            assertObject(pair, "inspection request pair");
            assertKeys(pair, { source_element_id: true, frame: true }, "inspection request pair");
            source = pair.source_element_id;
            frame = pair.frame;
            assertIdentifier(source, "requested source id");
            if (!hasOwn(seenSources, source)) { seenSources[source] = true; }
            if (!isFinite(frame) || typeof frame !== "number" || Math.floor(frame) !== frame || frame < 0 || frame >= frameCount) {
                throw new Error("requested frame is outside bounds");
            }
            if (!hasOwn(seenPairs, source + "\u0000" + String(frame))) {
                seenPairs[source + "\u0000" + String(frame)] = true;
            } else {
                throw new Error("inspection request contains duplicate source/frame pairs");
            }
        }
        for (source in seenSources) {
            if (hasOwn(seenSources, source) && !hasOwnValue(layerSources, source)) {
                throw new Error("requested source is not in the authoritative inventory");
            }
        }
        return {
            frame_count: frameCount,
            fps: fps,
            requested: requested,
            layer_sources: layerSources,
            layer_native_ids: layerNativeIds,
            manual_ids: manualIds
        };
    }

    function hasOwnValue(inventory, sourceId) {
        var key;
        for (key in inventory) {
            if (hasOwn(inventory, key) && inventory[key] === sourceId) { return true; }
        }
        return false;
    }

    function layerIsCameraOrLight(layer) {
        if (typeof CameraLayer !== "undefined" && layer instanceof CameraLayer) { return true; }
        if (typeof LightLayer !== "undefined" && layer instanceof LightLayer) { return true; }
        return false;
    }

    function expectedLayerComment(instanceId, sourceId) {
        return "keepframe:layer=" + instanceId + ";source_element_id=" + (sourceId || "");
    }

    function layerCommentLooksMapped(comment) {
        return comment.indexOf("keepframe:layer=") === 0 || comment.indexOf(";source_element_id=") >= 0;
    }

    function inspectLayerInventory(request, comp) {
        var rows = [];
        var seen = {};
        var seenNative = {};
        var index;
        var layer;
        var metadata;
        var comment;
        var instanceId;
        var sourceId;
        var nativeId;
        var manualIndex = 0;
        var manualAssignments = [];
        var expected;
        var frameStart;
        var frameEnd;
        if (typeof comp.numLayers !== "number" || !isFinite(comp.numLayers) || comp.numLayers > 1000) {
            throw new Error("layer inventory exceeds 1000 layers");
        }
        function frameBoundary(time) {
            var value = Number(time) * request.fps;
            if (!isFinite(value)) { throw new Error("layer frame interval is invalid"); }
            return Math.max(0, Math.min(request.frame_count, Math.ceil(value - 0.000000001)));
        }
        for (index = 1; index <= comp.numLayers; index += 1) {
            layer = comp.layer(index);
            comment = String(layer.comment || "");
            metadata = layerMetadata(layer);
            nativeId = nativeLayerId(layer);
            if (hasOwn(seenNative, String(nativeId))) {
                throw new Error("duplicate native layer id");
            }
            seenNative[String(nativeId)] = true;
            if (metadata) {
                instanceId = metadata.layer_instance_id;
                sourceId = metadata.source_element_id;
                if (!hasOwn(request.layer_sources, instanceId)) {
                    throw new Error("mapped layer source drift: unknown instance");
                }
                if (request.layer_sources[instanceId] !== sourceId) {
                    throw new Error("mapped layer source drift");
                }
                if (hasOwn(request.layer_native_ids, instanceId)) {
                    if (request.layer_native_ids[instanceId] !== nativeId) {
                        throw new Error("native layer id does not match the authoritative inventory");
                    }
                } else if (
                    inventorySize(request.layer_native_ids) > 0
                    && request.layer_sources[instanceId] !== null
                ) {
                    throw new Error("authoritative native layer inventory is incomplete");
                }
                expected = expectedLayerComment(instanceId, sourceId);
                if (comment !== expected) {
                    throw new Error("mapped layer comment does not match the authoritative inventory");
                }
                if (hasOwn(seen, instanceId)) {
                    throw new Error("duplicate mapped layer instance id");
                }
                seen[instanceId] = true;
            } else {
                if (layerIsCameraOrLight(layer)) { continue; }
                if (layerCommentLooksMapped(comment)) {
                    throw new Error("mapped layer comment is invalid or spoofed");
                }
                if (manualIndex >= request.manual_ids.length) {
                    throw new Error("allow_new_agent_ids pool is exhausted");
                }
                instanceId = request.manual_ids[manualIndex];
                manualIndex += 1;
                if (hasOwn(seen, instanceId) || hasOwn(request.layer_sources, instanceId)) {
                    throw new Error("duplicate manual agent id");
                }
                sourceId = null;
                manualAssignments.push({ layer: layer, instance_id: instanceId });
                seen[instanceId] = true;
            }
            frameStart = frameBoundary(layer.inPoint);
            frameEnd = frameBoundary(layer.outPoint);
            if (frameEnd <= frameStart) { throw new Error("layer frame interval is empty or reversed"); }
            rows.push({
                layer_instance_id: instanceId,
                native_layer_id: nativeId,
                index: index,
                frame_start: frameStart,
                frame_end: frameEnd,
                source_element_id: sourceId
            });
        }
        for (instanceId in request.layer_sources) {
            if (
                hasOwn(request.layer_sources, instanceId)
                && !hasOwn(seen, instanceId)
                && request.layer_sources[instanceId] !== null
            ) {
                throw new Error("authoritative layer inventory is incomplete");
            }
        }
        for (index = 0; index < manualAssignments.length; index += 1) {
            manualAssignments[index].layer.comment = expectedLayerComment(manualAssignments[index].instance_id, null);
        }
        return rows;
    }

    function finiteCompPoint(value) {
        return (
            value
            && value.length >= 2
            && isFinite(Number(value[0]))
            && isFinite(Number(value[1]))
            && Math.abs(Number(value[0])) <= 1000000
            && Math.abs(Number(value[1])) <= 1000000
        );
    }

    function unwrapWorldRotation(rawRotation, rotationState) {
        var delta;
        if (!rotationState) { return rawRotation; }
        if (
            typeof rotationState.raw !== "number"
            || typeof rotationState.unwrapped !== "number"
        ) {
            rotationState.raw = rawRotation;
            rotationState.unwrapped = rawRotation;
            return rawRotation;
        }
        delta = rawRotation - rotationState.raw;
        while (delta > 180) { delta -= 360; }
        while (delta < -180) { delta += 360; }
        rotationState.raw = rawRotation;
        rotationState.unwrapped += delta;
        return rotationState.unwrapped;
    }


    function inspectModelTransformSample(layer, time) {
        try {
            var transform = layer.transform;
            var position = transform.position.valueAtTime(time, false);
            var scale = transform.scale.valueAtTime(time, false);
            var effects = layer.property("ADBE Effect Parade");
            var wipe = effects ? effects.property("ADBE Linear Wipe") : null;
            var sample = {
                x: Number(position[0]), y: Number(position[1]),
                sx: Number(scale[0]) / 100, sy: Number(scale[1]) / 100,
                rot: Number(transform.zRotation.valueAtTime(time, false)),
                opacity: Number(transform.opacity.valueAtTime(time, false)) / 100,
                rotation_x: Number(transform.xRotation.valueAtTime(time, false)),
                rotation_y: Number(transform.yRotation.valueAtTime(time, false)),
                wipe_completion: wipe ? Number(wipe.property("ADBE Linear Wipe-0001").valueAtTime(time, false)) : 0,
                xmin: null, ymin: null, xmax: null, ymax: null
            };
            var key;
            for (key in sample) {
                if (hasOwn(sample, key) && sample[key] !== null && (!isFinite(sample[key]) || Math.abs(sample[key]) > 1000000)) { return null; }
            }
            if (sample.opacity < 0 || sample.opacity > 1 || sample.wipe_completion < 0 || sample.wipe_completion > 100) { return null; }
            // ponytail: local model transforms preserve IR motion channels;
            // projected model bounds require host sourceRectAtTime/toComp support.
            return sample;
        } catch (ignored) {
            return null;
        }
    }

    function inspectLayerSample(layer, time, rotationState) {
        var modelSample = layer.threeDLayer ? inspectModelTransformSample(layer, time) : null;
        var transform;
        var anchor;
        var origin;
        var basisX;
        var basisY;
        var anchorWorld;
        var rect;
        var corners = [];
        var point;
        var index;
        var sx;
        var sy;
        var rotation;
        var opacity;
        var xmin = null;
        var ymin = null;
        var xmax = null;
        var ymax = null;
        try {
            if (typeof layer.toComp !== "function" || typeof layer.sourceRectAtTime !== "function") {
                return modelSample;
            }
            transform = layer.transform;
            anchor = transform.anchorPoint.valueAtTime(time, false);
            if (!anchor || anchor.length < 2) { return modelSample; }
            origin = layer.toComp([0, 0], time);
            basisX = layer.toComp([1, 0], time);
            basisY = layer.toComp([0, 1], time);
            anchorWorld = layer.toComp([Number(anchor[0]), Number(anchor[1])], time);
            if (!finiteCompPoint(origin) || !finiteCompPoint(basisX) || !finiteCompPoint(basisY) || !finiteCompPoint(anchorWorld)) {
                return modelSample;
            }
            rect = layer.sourceRectAtTime(time, false);
            if (!rect || !isFinite(Number(rect.left)) || !isFinite(Number(rect.top)) || !isFinite(Number(rect.width)) || !isFinite(Number(rect.height))) {
                return modelSample;
            }
            corners.push([Number(rect.left), Number(rect.top)]);
            corners.push([Number(rect.left) + Number(rect.width), Number(rect.top)]);
            corners.push([Number(rect.left), Number(rect.top) + Number(rect.height)]);
            corners.push([Number(rect.left) + Number(rect.width), Number(rect.top) + Number(rect.height)]);
            for (index = 0; index < corners.length; index += 1) {
                point = layer.toComp(corners[index], time);
                if (!finiteCompPoint(point)) { return modelSample; }
                xmin = xmin === null ? Number(point[0]) : Math.min(xmin, Number(point[0]));
                ymin = ymin === null ? Number(point[1]) : Math.min(ymin, Number(point[1]));
                xmax = xmax === null ? Number(point[0]) : Math.max(xmax, Number(point[0]));
                ymax = ymax === null ? Number(point[1]) : Math.max(ymax, Number(point[1]));
            }
            sx = Math.sqrt(
                Math.pow(Number(basisX[0]) - Number(origin[0]), 2)
                + Math.pow(Number(basisX[1]) - Number(origin[1]), 2)
            );
            sy = Math.sqrt(
                Math.pow(Number(basisY[0]) - Number(origin[0]), 2)
                + Math.pow(Number(basisY[1]) - Number(origin[1]), 2)
            );
            var determinant = (
                (Number(basisX[0]) - Number(origin[0])) * (Number(basisY[1]) - Number(origin[1]))
                - (Number(basisX[1]) - Number(origin[1])) * (Number(basisY[0]) - Number(origin[0]))
            );
            if (!isFinite(determinant)) { return modelSample; }
            if (determinant < 0) { sy = -sy; }
            rotation = Math.atan2(Number(basisX[1]) - Number(origin[1]), Number(basisX[0]) - Number(origin[0])) * 180 / Math.PI;
            rotation = unwrapWorldRotation(rotation, rotationState);
            opacity = Number(transform.opacity.valueAtTime(time, false)) / 100;
            var wipe = layer.property("ADBE Effect Parade").property("ADBE Linear Wipe");
            var completion = wipe ? Number(wipe.property("ADBE Linear Wipe-0001").valueAtTime(time, false)) : 0;
            if (!isFinite(completion) || completion < 0 || completion > 100) { return modelSample; }
            if (!isFinite(sx) || !isFinite(sy) || Math.abs(sx) > 1000000 || Math.abs(sy) > 1000000 || !isFinite(rotation) || Math.abs(rotation) > 1000000 || !isFinite(opacity) || opacity < 0 || opacity > 1) {
                return modelSample;
            }
            var sample = {
                x: Number(anchorWorld[0]),
                y: Number(anchorWorld[1]),
                sx: sx,
                sy: sy,
                rot: rotation,
                opacity: opacity,
                wipe_completion: completion,
                xmin: xmin,
                ymin: ymin,
                xmax: xmax,
                ymax: ymax
            };
            if (modelSample) {
                modelSample.xmin = sample.xmin;
                modelSample.ymin = sample.ymin;
                modelSample.xmax = sample.xmax;
                modelSample.ymax = sample.ymax;
                return modelSample;
            }
            return sample;
        } catch (ignored) {
            return modelSample;
        }
    }

    function inspectLayers(payload) {
        var comp = hasOwn(payload, "checkpoint_index")
            ? reopenImmutableCheckpoint(payload.checkpoint_index)
            : requireSessionComposition();
        var originalComments = [];
        var snapshotIndex;
        for (snapshotIndex = 1; snapshotIndex <= comp.numLayers; snapshotIndex += 1) {
            originalComments.push({
                layer: comp.layer(snapshotIndex),
                comment: String(comp.layer(snapshotIndex).comment || "")
            });
        }
        try {
            var request = validateInspectRequest(payload, comp);
            var liveHeartbeat = heartbeat(false);
            var rows = inspectLayerInventory(request, comp);
        var layerSources = {};
        var layerNativeIds = {};
        var samples = [];
        var sampleRows = [];
        var missingPairs = [];
        var missingByIndex = [];
        var requestOrder = [];
        var rotationStates = {};
        var pairIndex;
        var orderIndex;
        var layerIndex;
        var rowIndex;
        var pair;
        var layer;
        var metadata;
        var instances;
        var sample;
        var active;
        var authoritativeLayer;
        var activeLayer;
        var pairMissing;
        var response;
        for (rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
            layerSources[rows[rowIndex].layer_instance_id] = rows[rowIndex].source_element_id;
            layerNativeIds[rows[rowIndex].layer_instance_id] = rows[rowIndex].native_layer_id;
        }
        for (pairIndex = 0; pairIndex < request.requested.length; pairIndex += 1) {
            requestOrder.push(pairIndex);
        }
        requestOrder.sort(function (leftIndex, rightIndex) {
            var leftFrame = Number(request.requested[leftIndex].frame);
            var rightFrame = Number(request.requested[rightIndex].frame);
            if (leftFrame !== rightFrame) { return leftFrame - rightFrame; }
            return leftIndex - rightIndex;
        });
        for (orderIndex = 0; orderIndex < requestOrder.length; orderIndex += 1) {
            pairIndex = requestOrder[orderIndex];
            pair = request.requested[pairIndex];
            instances = [];
            authoritativeLayer = false;
            activeLayer = false;
            pairMissing = false;
            for (layerIndex = 1; layerIndex <= comp.numLayers; layerIndex += 1) {
                layer = comp.layer(layerIndex);
                metadata = layerMetadata(layer);
                if (!metadata || metadata.source_element_id !== pair.source_element_id) {
                    continue;
                }
                authoritativeLayer = true;
                active = layer.enabled !== false
                    && Number(pair.frame) / request.fps >= Number(layer.inPoint)
                    && Number(pair.frame) / request.fps < Number(layer.outPoint);
                if (!rotationStates[metadata.layer_instance_id]) {
                    rotationStates[metadata.layer_instance_id] = {};
                }
                sample = active
                    ? inspectLayerSample(
                        layer,
                        Number(pair.frame) / request.fps,
                        rotationStates[metadata.layer_instance_id]
                    )
                    : null;
                if (active) {
                    activeLayer = true;
                    if (sample === null) { pairMissing = true; }
                }
                instances.push({
                    layer_instance_id: metadata.layer_instance_id,
                    active: active,
                    sample: sample === null ? null : sample
                });
            }
            if (!authoritativeLayer || (activeLayer && pairMissing)) {
                missingByIndex[pairIndex] = true;
                pairMissing = true;
            }
            sampleRows[pairIndex] = {
                source_element_id: pair.source_element_id,
                frame: pair.frame,
                instances: instances,
                missing: pairMissing
            };
        }
        for (pairIndex = 0; pairIndex < request.requested.length; pairIndex += 1) {
            if (missingByIndex[pairIndex]) {
                missingPairs.push({
                    source_element_id: request.requested[pairIndex].source_element_id,
                    frame: request.requested[pairIndex].frame
                });
            }
        }
        samples = sampleRows;
        response = {
            schema_version: "keepframe.ae-inspection/1",
            frame_count: request.frame_count,
            fps: request.fps,
            requested: request.requested,
            layers: rows,
            layer_sources: layerSources,
            layer_native_ids: layerNativeIds,
            samples: samples,
            missing_pairs: missingPairs,
            source_aggregated: false,
            heartbeat: {
                capability_hash: liveHeartbeat.capability_hash,
                capabilities: liveHeartbeat.capabilities,
                version: liveHeartbeat.version,
                major: liveHeartbeat.major,
                host: liveHeartbeat.host,
                ready: liveHeartbeat.ready,
                project_open: liveHeartbeat.project_open,
                timestamp: liveHeartbeat.timestamp
            }
        };
        if (JSON.stringify(response).length > MAX_JSON_BYTES) {
            throw new Error("inspection response exceeds 1 MiB");
        }
            return response;
        } catch (inspectError) {
            for (snapshotIndex = 0; snapshotIndex < originalComments.length; snapshotIndex += 1) {
                originalComments[snapshotIndex].layer.comment = originalComments[snapshotIndex].comment;
            }
            throw inspectError;
        }
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

    function previewFrameName(checkpointIndex, frame) {
        var padded = String(frame);
        while (padded.length < 6) { padded = "0" + padded; }
        return "checkpoint-" + String(checkpointIndex) + "-" + padded + ".png";
    }

    function renderPreview(payload) {
        var comp = requireSessionComposition();
        var checkpointIndex;
        var frameCount;
        var fps;
        var representative;
        var seen = {};
        var index;
        var frame;
        var expectedFrameCount;
        var factor = 1;
        var originalResolution;
        var restoreResolution;
        var file;
        var representativeFiles = [];
        var renderedWidth;
        var renderedHeight;
        assertObject(payload, "render_preview payload");
        assertKeys(payload, {
            project_id: true,
            plan_id: true,
            session_id: true,
            checkpoint_index: true,
            frame_count: true,
            fps: true,
            representative_frames: true
        }, "render_preview payload");
        checkpointIndex = payload.checkpoint_index;
        frameCount = payload.frame_count;
        fps = payload.fps;
        representative = payload.representative_frames;
        if (
            typeof checkpointIndex !== "number"
            || !isFinite(checkpointIndex)
            || checkpointIndex < 0
            || checkpointIndex > 1000000
            || Math.floor(checkpointIndex) !== checkpointIndex
        ) {
            throw new Error("checkpoint index is invalid");
        }
        if (
            typeof frameCount !== "number"
            || !isFinite(frameCount)
            || frameCount < 1
            || frameCount > MAX_KEYFRAMES
            || Math.floor(frameCount) !== frameCount
        ) {
            throw new Error("preview frame count is invalid");
        }
        if (typeof fps !== "number" || !isFinite(fps) || fps < 1 || fps > 99) {
            throw new Error("preview fps is invalid");
        }
        expectedFrameCount = comp.duration * comp.frameRate;
        if (!approximatelyEqual(fps, comp.frameRate) || !isFinite(expectedFrameCount) || !approximatelyEqual(frameCount, expectedFrameCount)) {
            throw new Error("preview timing does not match the bound composition");
        }
        if (!(representative instanceof Array) || representative.length < 1 || representative.length > 12) {
            throw new Error("representative frames are outside bounds");
        }
        for (index = 0; index < representative.length; index += 1) {
            frame = representative[index];
            if (typeof frame !== "number" || !isFinite(frame) || Math.floor(frame) !== frame || frame < 0 || frame >= frameCount) {
                throw new Error("representative frame is outside bounds");
            }
            if (hasOwn(seen, String(frame))) { throw new Error("representative frames contain duplicates"); }
            seen[String(frame)] = true;
        }
        if (!comp.resolutionFactor || comp.resolutionFactor.length < 2) {
            throw new Error("composition resolution is unavailable");
        }
        while (
            Math.ceil(comp.width / factor) > 1280
            || Math.ceil(comp.height / factor) > 720
        ) {
            factor *= 2;
            if (factor > 1000000) { throw new Error("preview resolution is outside bounds"); }
        }
        renderedWidth = Math.ceil(comp.width / factor);
        renderedHeight = Math.ceil(comp.height / factor);
        ensureFolders();
        originalResolution = comp.resolutionFactor;
        restoreResolution = [Number(originalResolution[0]), Number(originalResolution[1])];
        try {
            comp.resolutionFactor = [factor, factor];
            for (frame = 0; frame < frameCount; frame += 1) {
                file = File(RENDER_DIR.fsName + "/" + previewFrameName(checkpointIndex, frame));
                if (typeof comp.saveFrameToPng !== "function") {
                    throw new Error("After Effects frame export is unavailable");
                }
                comp.saveFrameToPng(frame / fps, file);
            }
            for (index = 0; index < representative.length; index += 1) {
                representativeFiles.push(previewFrameName(checkpointIndex, representative[index]));
            }
        } finally {
            comp.resolutionFactor = restoreResolution;
        }
        return {
            rendered: true,
            kind: "preview",
            checkpoint_index: checkpointIndex,
            frame_count: frameCount,
            fps: fps,
            width: renderedWidth,
            height: renderedHeight,
            resolution_factor: [factor, factor],
            sequence: {
                directory: "renders",
                pattern: "checkpoint-N-%06d.png",
                frame_count: frameCount,
                first_frame: previewFrameName(checkpointIndex, 0),
                last_frame: previewFrameName(checkpointIndex, frameCount - 1)
            },
            representative_frames: representative,
            representative_files: representativeFiles
        };
    }

    function finalFrameName(frame) {
        var padded = String(frame);
        while (padded.length < 6) { padded = "0" + padded; }
        return "final-" + padded + ".png";
    }

    function clearFinalSequence() {
        var files = RENDER_DIR.getFiles();
        var index;
        var file;
        for (index = 0; index < files.length; index += 1) {
            file = files[index];
            if (
                file instanceof File
                && /^final-[0-9]{6}\.png$/.test(String(file.name || ""))
                && !file.remove()
            ) {
                throw new Error("cannot clear the final render sequence");
            }
        }
    }

    function renderFinal(payload) {
        var comp = null;
        var checkpointIndex;
        var frameCount;
        var fps;
        var expectedFrameCount;
        var width;
        var height;
        var originalResolution;
        var restoreResolution;
        var queueItem = null;
        var outputModule;
        var outputFile;
        var frame;
        var file;
        var queueStates = [];
        var queueIndex;
        var existingQueueItem;
        assertObject(payload, "render_final payload");
        assertKeys(payload, {
            project_id: true,
            plan_id: true,
            session_id: true,
            checkpoint_index: true,
            frame_count: true,
            fps: true,
            width: true,
            height: true
        }, "render_final payload");
        checkpointIndex = payload.checkpoint_index;
        frameCount = payload.frame_count;
        fps = payload.fps;
        width = payload.width;
        height = payload.height;
        if (
            typeof checkpointIndex !== "number"
            || !isFinite(checkpointIndex)
            || checkpointIndex < 0
            || checkpointIndex > 1000000
            || Math.floor(checkpointIndex) !== checkpointIndex
        ) {
            throw new Error("checkpoint index is invalid");
        }
        comp = reopenImmutableCheckpoint(checkpointIndex);
        if (
            typeof frameCount !== "number"
            || !isFinite(frameCount)
            || frameCount < 1
            || frameCount > MAX_FINAL_FRAMES
            || Math.floor(frameCount) !== frameCount
        ) {
            throw new Error("final frame count is invalid");
        }
        if (typeof fps !== "number" || !isFinite(fps) || fps < 1 || fps > 99) {
            throw new Error("final fps is invalid");
        }
        if (
            typeof width !== "number"
            || typeof height !== "number"
            || !isFinite(width)
            || !isFinite(height)
            || width < 1
            || height < 1
            || Math.floor(width) !== width
            || Math.floor(height) !== height
            || width > 16384
            || height > 16384
            || width * height > 268435456
            || width !== comp.width
            || height !== comp.height
        ) {
            throw new Error("final dimensions do not match the bound composition");
        }
        if (frameCount * width * height > MAX_FINAL_TOTAL_PIXELS) {
            throw new Error("final render exceeds the resource budget");
        }
        expectedFrameCount = comp.duration * comp.frameRate;
        if (
            !approximatelyEqual(fps, comp.frameRate)
            || !isFinite(expectedFrameCount)
            || !approximatelyEqual(frameCount, expectedFrameCount)
        ) {
            throw new Error("final timing does not match the bound composition");
        }
        if (!comp.resolutionFactor || comp.resolutionFactor.length < 2) {
            throw new Error("composition resolution is unavailable");
        }
        ensureFolders();
        clearFinalSequence();
        originalResolution = comp.resolutionFactor;
        restoreResolution = [Number(originalResolution[0]), Number(originalResolution[1])];
        try {
            comp.resolutionFactor = [1, 1];
            if (
                !app.project.renderQueue
                || !app.project.renderQueue.items
                || typeof app.project.renderQueue.items.add !== "function"
            ) {
                throw new Error("After Effects Render Queue is unavailable");
            }
            for (
                queueIndex = 1;
                queueIndex <= app.project.renderQueue.numItems;
                queueIndex += 1
            ) {
                existingQueueItem = app.project.renderQueue.item(queueIndex);
                queueStates.push({
                    item: existingQueueItem,
                    render: Boolean(existingQueueItem.render)
                });
                if (existingQueueItem.render) {
                    existingQueueItem.render = false;
                }
            }
            queueItem = app.project.renderQueue.items.add(comp);
            queueItem.timeSpanStart = 0;
            queueItem.timeSpanDuration = comp.duration;
            if (typeof queueItem.outputModule !== "function") {
                throw new Error("After Effects output module is unavailable");
            }
            outputModule = queueItem.outputModule(1);
            if (!outputModule || typeof outputModule.applyTemplate !== "function") {
                throw new Error("PNG output module is unavailable");
            }
            outputModule.applyTemplate("PNG Sequence");
            outputFile = File(RENDER_DIR.fsName + "/final-[######].png");
            outputModule.file = outputFile;
            queueItem.render = true;
            app.project.renderQueue.render();
        } finally {
            comp.resolutionFactor = restoreResolution;
            for (queueIndex = 0; queueIndex < queueStates.length; queueIndex += 1) {
                queueStates[queueIndex].item.render = queueStates[queueIndex].render;
            }
            if (queueItem && typeof queueItem.remove === "function") {
                queueItem.remove();
            }
        }
        for (frame = 0; frame < frameCount; frame += 1) {
            file = File(RENDER_DIR.fsName + "/" + finalFrameName(frame));
            if (!file.exists || file.length <= 0) {
                throw new Error("final Render Queue sequence is incomplete");
            }
        }
        return {
            rendered: true,
            kind: "final",
            checkpoint_index: checkpointIndex,
            frame_count: frameCount,
            fps: fps,
            width: width,
            height: height,
            sequence: {
                directory: "renders",
                pattern: "final-%06d.png",
                frame_count: frameCount,
                first_frame: finalFrameName(0),
                last_frame: finalFrameName(frameCount - 1)
            }
        };
    }


    function packageProject(payload) {
        var checkpointIndex;
        var file;
        var comp;
        assertObject(payload, "package_project payload");
        assertKeys(payload, {
            selected_checkpoint: true,
            checkpoint_index: true
        }, "package_project payload");
        checkpointIndex = payload.selected_checkpoint;
        if (
            typeof checkpointIndex !== "number"
            || !isFinite(checkpointIndex)
            || checkpointIndex < 0
            || checkpointIndex > 1000000
            || Math.floor(checkpointIndex) !== checkpointIndex
        ) {
            throw new Error("selected checkpoint is invalid");
        }
        if (payload.checkpoint_index !== checkpointIndex) {
            throw new Error("package checkpoint binding does not match");
        }
        comp = reopenImmutableCheckpoint(checkpointIndex);
        if (!comp) {
            throw new Error("checkpoint session composition is unavailable");
        }
        ensureFolders();
        file = File(PACKAGE_DIR.fsName + "/project.aep");
        app.project.save(file);
        if (!file.exists || file.length <= 0) {
            throw new Error("scoped project AEP was not saved");
        }
        return {
            saved: true,
            kind: "package",
            filename: file.name,
            directory: "package",
            selected_checkpoint: checkpointIndex
        };
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
                return inspectLayers(command.payload || {});
            case "save_checkpoint":
                return saveCheckpoint(command.payload || {});
            case "render_preview":
                return renderPreview(command.payload || {});
            case "render_final":
                return renderFinal(command.payload || {});
            case "package_project":
                return packageProject(command.payload || {});
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
