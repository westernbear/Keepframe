"use strict";
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

// Keep implementation data outside proxies: JSX only sees the documented surface.
const records = new WeakMap();
const copy = (value) => JSON.parse(JSON.stringify(value));
const unsupported = (type, name) => { throw Error(`fake AE: unsupported ${type}.${String(name)}`); };
function host(type, target, record = {}, indexed) {
  if (typeof target === "function") Object.defineProperty(target, Symbol.hasInstance, {
    value: (value) => Function.prototype[Symbol.hasInstance].call(target, value),
  });
  const proxy = new Proxy(target, {
    get(object, name) {
      if (Object.hasOwn(object, name)) return Reflect.get(object, name);
      if (indexed && typeof name === "string" && /^[1-9]\d*$/.test(name)) {
        const value = indexed(Number(name));
        if (value !== undefined) return value;
      }
      return unsupported(type, name);
    },
    set(object, name, value) {
      const descriptor = Object.getOwnPropertyDescriptor(object, name);
      if (!descriptor) return unsupported(type, name);
      if (!descriptor.set) throw Error(`fake AE: read-only ${type}.${String(name)}`);
      descriptor.set(value);
      return true;
    },
    defineProperty(_object, name) { return unsupported(type, name); },
    deleteProperty(_object, name) { return unsupported(type, name); },
    setPrototypeOf() { return unsupported(type, "__proto__"); },
  });
  records.set(proxy, { type, ...record });
  return proxy;
}
function field(target, name, get, set) {
  Object.defineProperty(target, name, { configurable: true, enumerable: true, get, set });
}
function finite(value, name) {
  if (typeof value !== "number" || !Number.isFinite(value)) throw Error(`fake AE: invalid ${name}`);
}
function vector(value, size, name, color = false) {
  if (!Array.isArray(value) || value.length !== size) throw Error(`fake AE: invalid ${name} dimensions`);
  value.forEach((n) => {
    finite(n, name);
    if (color && (n < 0 || n > 1)) throw Error(`fake AE: invalid ${name} range`);
  });
}

function createAE({ state = {}, documents = process.cwd() } = {}) {
  const counters = { undoGroups: 0, writes: 0 };
  const changed = () => { counters.writes++; };
  const env = copy(state.app?.env ?? state.env ?? {});
  const fonts = copy(state.app?.fonts ?? state.fonts ?? []);
  function setting(target, values, name, validate = () => {}, count = true) {
    field(target, name, () => Array.isArray(values[name]) ? copy(values[name]) : values[name], (value) => {
      validate(value);
      values[name] = Array.isArray(value) ? copy(value) : value;
      if (count) changed();
    });
  }
  const enumObject = (type, names) => host(type, Object.fromEntries(names.map((name) => [name, name])));
  const KeyframeInterpolationType = enumObject("KeyframeInterpolationType", ["LINEAR", "BEZIER", "HOLD"]);
  const ParagraphJustification = enumObject("ParagraphJustification", ["LEFT_JUSTIFY", "CENTER_JUSTIFY", "RIGHT_JUSTIFY"]);
  const PropertyValueType = enumObject("PropertyValueType", ["NO_VALUE", "OneD", "TwoD", "TwoD_SPATIAL",
    "ThreeD", "ThreeD_SPATIAL", "COLOR", "TEXT_DOCUMENT"]);

  // File/Folder: ported from 2c1c8d1:tests/ae_panel_host.js, with strict surfaces.
  function File(p) {
    let filename = path.resolve(String(p));
    let mode = null, buffer = "", encoding = "BINARY";
    const api = Object.create(File.prototype);
    field(api, "fsName", () => filename);
    field(api, "name", () => path.basename(filename));
    field(api, "encoding", () => encoding, (value) => { encoding = String(value); });
    field(api, "exists", () => { try { return fs.statSync(filename).isFile(); } catch { return false; } });
    api.toString = () => filename;
    api[Symbol.toPrimitive] = () => filename;
    api.open = (nextMode) => {
      if (nextMode !== "r" && nextMode !== "w") return unsupported("File", `open(${nextMode})`);
      if (nextMode === "r") {
        try { buffer = fs.readFileSync(filename, "utf8"); } catch { return false; }
      } else buffer = "";
      mode = nextMode;
      return true;
    };
    api.read = () => buffer;
    api.write = (text) => { buffer += String(text); return true; };
    api.close = () => {
      if (mode === "w") fs.writeFileSync(filename, buffer, "utf8");
      mode = null;
      return true;
    };
    api.remove = () => { try { fs.unlinkSync(filename); return true; } catch { return false; } };
    api.rename = (name) => {
      const dest = path.join(path.dirname(filename), String(name));
      if (fs.existsSync(dest)) return false;
      try { fs.renameSync(filename, dest); } catch { return false; }
      filename = dest;
      return true;
    };
    return host("File", api);
  }
  function Folder(p) {
    const filename = path.resolve(String(p));
    const api = Object.create(Folder.prototype);
    field(api, "fsName", () => filename);
    field(api, "name", () => path.basename(filename));
    field(api, "exists", () => { try { return fs.statSync(filename).isDirectory(); } catch { return false; } });
    api.toString = () => filename;
    api[Symbol.toPrimitive] = () => filename;
    api.create = () => { fs.mkdirSync(filename, { recursive: true }); return true; };
    api.getFiles = () => fs.readdirSync(filename).map((name) => {
      const full = path.join(filename, name);
      return fs.statSync(full).isDirectory() ? Folder(full) : File(full);
    });
    return host("Folder", api);
  }
  Folder.myDocuments = Folder(documents);
  Folder.userData = Folder(env.LOCALAPPDATA || documents);
  function ImportOptions(file) {
    const values = { file };
    const api = Object.create(ImportOptions.prototype);
    setting(api, values, "file", (v) => {
      if (records.get(v)?.type !== "File") throw Error("fake AE: ImportOptions requires a File");
    }, false);
    if (records.get(file)?.type !== "File") throw Error("fake AE: ImportOptions requires a File");
    return host("ImportOptions", api);
  }

  // Detached values: changes to these copies do not modify a project property.
  function TextDocument(text, saved = {}) {
    const values = { text: String(text), font: "ArialMT", fontSize: 36, fillColor: [1, 1, 1],
      applyFill: true, justification: "LEFT_JUSTIFY", ...copy(saved) };
    const api = Object.create(TextDocument.prototype);
    for (const name of ["text", "font", "applyFill"]) setting(api, values, name, () => {}, false);
    setting(api, values, "fontSize", (v) => { finite(v, "fontSize"); if (v <= 0) throw Error("fake AE: invalid fontSize"); }, false);
    setting(api, values, "fillColor", (v) => vector(v, 3, "fillColor", true), false);
    setting(api, values, "justification", (v) => {
      if (!["LEFT_JUSTIFY", "CENTER_JUSTIFY", "RIGHT_JUSTIFY"].includes(v)) throw Error("fake AE: invalid justification");
    }, false);
    return host("TextDocument", api, { values });
  }
  function KeyframeEase(speed, influence) {
    const validateInfluence = (v) => {
      finite(v, "influence");
      if (v < 0.1 || v > 100) throw Error("fake AE: influence must be 0.1–100");
    };
    finite(speed, "speed"); validateInfluence(influence);
    const values = { speed, influence }, api = Object.create(KeyframeEase.prototype);
    setting(api, values, "speed", (v) => finite(v, "speed"), false);
    setting(api, values, "influence", validateInfluence, false);
    return host("KeyframeEase", api, { values });
  }

  // Properties: data is JSON-safe; TextDocuments/eases cross the API as copies.
  function property(matchName, name, value, valueType, saved) {
    const dimensions = Array.isArray(value) ? value.length : 1;
    const data = saved ? copy(saved) : { matchName, name, value: copy(value), keys: [],
      expression: "", expressionEnabled: false, propertyValueType: valueType || (dimensions === 2 ? "TwoD" : "OneD") };
    const api = {};
    const key = (i) => {
      if (!Number.isInteger(i) || i < 1 || i > data.keys.length) throw Error("fake AE: invalid key index");
      return data.keys[i - 1];
    };
    const encode = (v) => {
      if (data.propertyValueType === "TEXT_DOCUMENT") {
        if (records.get(v)?.type !== "TextDocument") throw Error("fake AE: expected TextDocument");
        return copy(records.get(v).values);
      }
      if (Array.isArray(data.value)) vector(v, data.value.length, name);
      else finite(v, name);
      return copy(v);
    };
    const decode = (v) => data.propertyValueType === "TEXT_DOCUMENT" ? TextDocument(v.text, v) : copy(v);
    const sample = (t) => {
      finite(t, "key time");
      const keys = data.keys;
      if (!keys.length) return copy(data.value);
      if (t <= keys[0].time) return copy(keys[0].value);
      for (let i = 1; i < keys.length; i++) {
        if (t === keys[i].time) return copy(keys[i].value);
        if (t < keys[i].time) {
          const a = keys[i - 1], b = keys[i];
          if (a.outInterpolation === "HOLD" || data.propertyValueType === "TEXT_DOCUMENT") return copy(a.value);
          // ponytail: BEZIER samples linearly; implement curves if sync starts sampling them.
          const fraction = (t - a.time) / (b.time - a.time);
          const interpolate = (x, y) => x + (y - x) * fraction;
          return Array.isArray(a.value) ? a.value.map((v, j) => interpolate(v, b.value[j])) : interpolate(a.value, b.value);
        }
      }
      return copy(keys[keys.length - 1].value);
    };
    field(api, "value", () => api.valueAtTime(0, false));
    field(api, "numKeys", () => data.keys.length);
    field(api, "matchName", () => data.matchName);
    field(api, "name", () => data.name);
    field(api, "propertyValueType", () => data.propertyValueType);
    field(api, "canSetExpression", () => true);
    field(api, "expression", () => data.expression, (v) => {
      if (typeof v !== "string") throw Error("fake AE: expression must be a string");
      data.expression = v; data.expressionEnabled = v !== ""; changed();
    });
    setting(api, data, "expressionEnabled", (v) => { if (typeof v !== "boolean") throw Error("fake AE: invalid expressionEnabled"); });
    api.setValue = (v) => {
      if (data.keys.length) throw Error("fake AE: setValue on a keyframed property");
      data.value = encode(v); changed();
    };
    api.setValueAtTime = (t, v) => {
      finite(t, "key time");
      const encoded = encode(v), existing = data.keys.find((k) => k.time === t);
      if (existing) existing.value = encoded;
      else {
        const ease = () => Array.from({ length: dimensions }, () => ({ speed: 0, influence: 33.33333333333333 }));
        data.keys.push({ time: t, value: encoded, inInterpolation: "LINEAR", outInterpolation: "LINEAR", inEases: ease(), outEases: ease() });
        data.keys.sort((a, b) => a.time - b.time);
      }
      changed();
    };
    api.keyTime = (i) => key(i).time;
    api.keyValue = (i) => decode(key(i).value);
    api.removeKey = (i) => { const removed = key(i); data.value = copy(removed.value); data.keys.splice(i - 1, 1); changed(); };
    api.nearestKeyIndex = (t) => {
      finite(t, "key time");
      if (!data.keys.length) throw Error("fake AE: nearestKeyIndex on an unkeyed property");
      let nearest = 0;
      data.keys.forEach((k, i) => { if (Math.abs(k.time - t) < Math.abs(data.keys[nearest].time - t)) nearest = i; });
      return nearest + 1;
    };
    api.valueAtTime = (t, preExpression) => {
      if (data.expressionEnabled && !preExpression) return unsupported("Property", "expression evaluation");
      return decode(sample(t));
    };
    api.setInterpolationTypeAtKey = (i, incoming, outgoing = incoming) => {
      const k = key(i);
      for (const v of [incoming, outgoing]) if (!["LINEAR", "BEZIER", "HOLD"].includes(v)) throw Error("fake AE: invalid interpolation type");
      k.inInterpolation = incoming; k.outInterpolation = outgoing; changed();
    };
    api.keyInInterpolationType = (i) => key(i).inInterpolation;
    api.keyOutInterpolationType = (i) => key(i).outInterpolation;
    api.setTemporalEaseAtKey = (i, incoming, outgoing = incoming) => {
      const k = key(i);
      const encodeEases = (eases) => {
        if (!Array.isArray(eases) || eases.length !== dimensions) throw Error("fake AE: temporal ease dimensions must match value dimensions");
        return eases.map((e) => {
          if (records.get(e)?.type !== "KeyframeEase") throw Error("fake AE: expected KeyframeEase");
          return copy(records.get(e).values);
        });
      };
      const inEases = encodeEases(incoming), outEases = encodeEases(outgoing);
      k.inEases = inEases; k.outEases = outEases; changed();
    };
    const decodeEases = (eases) => eases.map((e) => KeyframeEase(e.speed, e.influence));
    api.keyInTemporalEase = (i) => decodeEases(key(i).inEases);
    api.keyOutTemporalEase = (i) => decodeEases(key(i).outEases);
    return host("Property", api, { data, api });
  }

  // Groups/effects/masks use the same 1-based property lookup.
  const effectDefinitions = {
    "ADBE Linear Wipe": ["Linear Wipe", [["0001", "Transition Completion", 0], ["0002", "Wipe Angle", 90], ["0003", "Feather", 0]]],
    "ADBE Geometry2": ["Transform", [["0001", "Anchor Point", [0, 0]], ["0002", "Position", [0, 0]],
      ["0003", "Scale Height", 100], ["0004", "Scale Width", 100], ["0005", "Skew", 0],
      ["0006", "Skew Axis", 0], ["0007", "Rotation", 0], ["0008", "Opacity", 100], ["0011", "Uniform Scale", 1]]],
  };
  function group(matchName, name, children = [], additions, saved) {
    const values = { name: saved?.name ?? name }, api = {};
    field(api, "matchName", () => matchName);
    setting(api, values, "name", (v) => { if (typeof v !== "string") throw Error("fake AE: invalid name"); });
    field(api, "numProperties", () => children.length);
    api.property = (query) => {
      const result = typeof query === "number" ? children[query - 1] : children.find((p) => p.matchName === query || p.name === query);
      return result ?? unsupported("PropertyGroup", query);
    };
    api.addProperty = (match) => {
      if (!additions?.includes(match)) return unsupported("PropertyGroup", match);
      const definition = effectDefinitions[match];
      const child = group(match, definition?.[0] || `Mask ${children.length + 1}`,
        definition ? definition[1].map(([suffix, label, value]) => property(`${match}-${suffix}`, label, value)) : []);
      removable(child);
      children.push(child); changed(); return child;
    };
    const proxy = host("PropertyGroup", api, { api, values, children, matchName });
    if (saved) {
      for (const childState of saved.properties) {
        const child = restoreProperty(childState);
        if (additions) removable(child);
        children.push(child);
      }
    }
    return proxy;
    function removable(child) {
      records.get(child).api.remove = () => {
        const index = children.indexOf(child);
        if (index < 0) throw Error("fake AE: removed property group");
        children.splice(index, 1); changed();
      };
    }
  }
  function restoreProperty(saved) {
    return saved.properties ? group(saved.matchName, saved.name, [], undefined, saved)
      : property(saved.matchName, saved.name, saved.value, saved.propertyValueType, saved);
  }
  function serializeProperty(p) {
    const r = records.get(p);
    if (r.type === "Property") return copy(r.data);
    return { matchName: r.matchName, name: r.values.name, properties: r.children.map(serializeProperty) };
  }

  // Layer transforms, including scalar followers for separated position.
  function transformGroup(layerValues, comp, source, saved) {
    const definitions = [
      ["ADBE Anchor Point", "Anchor Point", source ? [source.width / 2, source.height / 2] : [0, 0], "TwoD_SPATIAL"],
      ["ADBE Position", "Position", [comp.width / 2, comp.height / 2], "TwoD_SPATIAL"],
      ["ADBE Scale", "Scale", [100, 100], "TwoD"], ["ADBE Rotate Z", "Rotation", 0],
      ["ADBE Opacity", "Opacity", 100], ["ADBE Rotate X", "X Rotation", 0],
      ["ADBE Rotate Y", "Y Rotation", 0], ["ADBE Orientation", "Orientation", [0, 0, 0], "ThreeD"],
      ["ADBE Position_0", "X Position", comp.width / 2], ["ADBE Position_1", "Y Position", comp.height / 2],
    ];
    const children = definitions.map(([match, name, value, valueType]) => {
      const old = saved?.properties.find((p) => p.matchName === match);
      return property(match, name, value, valueType, old);
    });
    const g = group("ADBE Transform Group", saved?.name || "Transform", children);
    const position = children[1], x = children[8], y = children[9];
    const r = records.get(position), xr = records.get(x), yr = records.get(y);
    r.data.dimensionsSeparated ??= false;
    const sample = r.api.valueAtTime, setValue = r.api.setValue, setValueAtTime = r.api.setValueAtTime;
    const visible = () => children.filter((p, i) => (i < 5 || (i < 8 && layerValues.threeDLayer) || (i >= 8 && r.data.dimensionsSeparated)));
    records.get(g).api.property = (query) => {
      const list = visible();
      const result = typeof query === "number" ? list[query - 1] : list.find((p) => p.matchName === query || p.name === query);
      return result ?? unsupported("PropertyGroup", query);
    };
    field(records.get(g).api, "numProperties", () => visible().length);
    field(r.api, "dimensionsSeparated", () => r.data.dimensionsSeparated, (value) => {
      if (typeof value !== "boolean") throw Error("fake AE: invalid dimensionsSeparated");
      if (value !== r.data.dimensionsSeparated) {
        if (value) {
          [xr, yr].forEach((follower, dimension) => {
            follower.data.value = r.data.value[dimension];
            follower.data.keys = r.data.keys.map((k) => ({ ...copy(k), value: k.value[dimension],
              inEases: [copy(k.inEases[dimension])], outEases: [copy(k.outEases[dimension])] }));
          });
          r.data.keys = [];
        } else {
          r.data.value = [xr.data.value, yr.data.value];
          const times = [...new Set([...xr.data.keys, ...yr.data.keys].map((k) => k.time))].sort((a, b) => a - b);
          r.data.keys = times.map((t) => {
            const a = xr.data.keys.find((k) => k.time === t), b = yr.data.keys.find((k) => k.time === t);
            const base = a || b, fallback = { speed: 0, influence: 33.33333333333333 };
            return { ...copy(base), value: [x.valueAtTime(t, true), y.valueAtTime(t, true)],
              inEases: [copy(a?.inEases[0] || fallback), copy(b?.inEases[0] || fallback)],
              outEases: [copy(a?.outEases[0] || fallback), copy(b?.outEases[0] || fallback)] };
          });
        }
        r.data.dimensionsSeparated = value;
      }
      changed();
    });
    r.api.valueAtTime = (t, pre) => r.data.dimensionsSeparated ? [x.valueAtTime(t, pre), y.valueAtTime(t, pre)] : sample(t, pre);
    field(r.api, "value", () => r.api.valueAtTime(0, false));
    r.api.setValue = (value) => {
      if (r.data.dimensionsSeparated) throw Error("fake AE: setValue on separated position");
      setValue(value);
    };
    r.api.setValueAtTime = (t, value) => {
      if (r.data.dimensionsSeparated) throw Error("fake AE: setValueAtTime on separated position");
      setValueAtTime(t, value);
    };
    return g;
  }

  // Project items and their collections. References serialize as 1-based indices.
  function CompItem() { return unsupported("CompItem", "constructor"); }
  function FootageItem() { return unsupported("FootageItem", "constructor"); }
  function FolderItem() { return unsupported("FolderItem", "constructor"); }
  function AVLayer() { return unsupported("AVLayer", "constructor"); }
  function TextLayer() { return unsupported("TextLayer", "constructor"); }
  Object.setPrototypeOf(TextLayer.prototype, AVLayer.prototype);
  const items = [];
  let root;
  const constructors = { CompItem, FootageItem, FolderItem };
  function collection(type, list, methods = {}) {
    const api = { ...methods };
    field(api, "length", () => list().length);
    return host(type, api, {}, (index) => list()[index - 1]);
  }
  function itemCollection(parent) {
    const add = (type, saved) => {
      const created = item(type, saved);
      records.get(created).values.parentFolder = parent || root;
      items.push(created); changed(); return created;
    };
    return collection("ItemCollection", () => parent ? items.filter((child) => child.parentFolder === parent) : items, {
      addComp(name, width, height, pixelAspect, duration, frameRate) {
        return add("CompItem", { name, width, height, pixelAspect, duration, frameRate });
      },
      addFolder(name) { return add("FolderItem", { name }); },
    });
  }
  function item(type, saved) {
    const values = { name: "", comment: "", ...copy(saved), parentFolder: root || null };
    const api = Object.create(constructors[type].prototype);
    setting(api, values, "name"); setting(api, values, "comment");
    field(api, "parentFolder", () => values.parentFolder, (folder) => {
      if (records.get(folder)?.type !== "FolderItem") throw Error("fake AE: parentFolder must be a FolderItem");
      for (let ancestor = folder; ancestor; ancestor = ancestor.parentFolder) {
        if (ancestor === proxy) throw Error("fake AE: cyclic parentFolder");
      }
      values.parentFolder = folder; changed();
    });
    const proxy = host(type, api, { values, api });
    if (type === "FolderItem") {
      api.items = itemCollection(proxy);
    } else {
      for (const name of ["width", "height"]) {
        values[name] ??= name === "width" ? 1920 : 1080;
        if (type === "CompItem") setting(api, values, name, (v) => { finite(v, name); if (!Number.isInteger(v) || v < 1) throw Error(`fake AE: invalid ${name}`); });
        else field(api, name, () => values[name]);
      }
      if (type === "FootageItem") {
        const source = saved.mainSource || { file: null };
        values.isModel = Boolean(saved.isModel);
        values.mainSource = { ...copy(source), file: source.file ? File(source.file) : null };
        const sourceAPI = {};
        field(sourceAPI, "file", () => values.mainSource.file);
        if (source.color) field(sourceAPI, "color", () => copy(values.mainSource.color));
        api.mainSource = host(source.color ? "SolidSource" : "FileSource", sourceAPI);
        api.replace = (file) => {
          if (records.get(file)?.type !== "File") throw Error("fake AE: replace requires a File");
          values.mainSource.file = file;
          values.isModel = /\.glb$/i.test(file.name);
          changed();
        };
      } else {
        values.frameRate ??= 30; values.duration ??= 5; values.pixelAspect ??= 1; values.bgColor ??= [0, 0, 0];
        for (const name of ["frameRate", "duration", "pixelAspect"]) setting(api, values, name, (v) => {
          finite(v, name); if (v <= 0) throw Error(`fake AE: invalid ${name}`);
        });
        setting(api, values, "bgColor", (v) => vector(v, 3, "bgColor", true));
        const layers = [];
        records.get(proxy).layers = layers;
        const addLayer = (layerType, sourceItem, name, duration, text) => {
          const l = layer(proxy, layerType, { name, outPoint: duration ?? proxy.duration, text }, sourceItem);
          layers.unshift(l); changed(); return l;
        };
        const solid = (color, name, w, h) => {
          vector(color, 3, "color", true);
          const footage = item("FootageItem", { name, width: w, height: h, mainSource: { file: null, color } });
          items.push(footage); return footage;
        };
        api.layers = collection("LayerCollection", () => layers, {
          add(sourceItem) {
            if (!["FootageItem", "CompItem"].includes(records.get(sourceItem)?.type)) throw Error("fake AE: layers.add requires footage or comp");
            return addLayer("AVLayer", sourceItem, sourceItem.name);
          },
          addText(text) { return addLayer("TextLayer", null, String(text), undefined, String(text)); },
          addSolid(color, name, w, h, pixelAspect, duration) {
            return addLayer("AVLayer", solid(color, name, w, h), name, duration);
          },
          addNull(duration) { return addLayer("AVLayer", solid([1, 1, 1], "Null", 100, 100), "Null", duration); },
        });
      }
    }
    return proxy;
  }
  root = item("FolderItem", state.project?.rootFolder || { name: "Root" });

  // Layers: collections store stack order, so index is always computed live.
  function layer(comp, type, saved, source) {
    const values = { name: "", comment: "", label: 0, inPoint: 0, outPoint: comp.duration,
      startTime: 0, threeDLayer: Boolean(source && records.get(source).values.isModel), ...copy(saved) };
    const api = Object.create((type === "TextLayer" ? TextLayer : AVLayer).prototype);
    for (const name of ["name", "comment"]) setting(api, values, name);
    setting(api, values, "label", (v) => { if (!Number.isInteger(v) || v < 0 || v > 16) throw Error("fake AE: label must be 0–16"); });
    for (const name of ["inPoint", "outPoint", "startTime"]) setting(api, values, name, (v) => finite(v, name));
    setting(api, values, "threeDLayer", (v) => { if (typeof v !== "boolean") throw Error("fake AE: invalid threeDLayer"); });
    const stack = records.get(comp).layers;
    field(api, "index", () => stack.indexOf(proxy) + 1);
    field(api, "source", () => source || null);
    const findGroup = (match) => saved.properties?.find((p) => p.matchName === match);
    const groups = [transformGroup(values, comp, source, findGroup("ADBE Transform Group")),
      group("ADBE Effect Parade", "Effects", [], Object.keys(effectDefinitions), findGroup("ADBE Effect Parade")),
      group("ADBE Mask Parade", "Masks", [], ["ADBE Mask Atom"], findGroup("ADBE Mask Parade"))];
    if (type === "TextLayer") {
      const savedText = findGroup("ADBE Text Properties");
      groups.push(savedText ? restoreProperty(savedText) : group("ADBE Text Properties", "Text", [
        property("ADBE Text Document", "Source Text", records.get(TextDocument(saved.text || "")).values, "TEXT_DOCUMENT"),
      ]));
    }
    api.property = (query) => {
      const result = typeof query === "number" ? groups[query - 1] : groups.find((g) => g.matchName === query || g.name === query);
      return result ?? unsupported(type, query);
    };
    field(api, "Effects", () => groups[1]); field(api, "Masks", () => groups[2]);
    const attached = () => { if (!stack.includes(proxy)) throw Error("fake AE: removed layer"); };
    const move = (target, offset) => {
      attached();
      if (!stack.includes(target)) throw Error("fake AE: move target must be in the same comp");
      if (target !== proxy) { stack.splice(stack.indexOf(proxy), 1); stack.splice(stack.indexOf(target) + offset, 0, proxy); }
      changed();
    };
    api.remove = () => { attached(); stack.splice(stack.indexOf(proxy), 1); changed(); };
    api.moveBefore = (target) => move(target, 0);
    api.moveAfter = (target) => move(target, 1);
    api.moveToBeginning = () => { attached(); stack.splice(stack.indexOf(proxy), 1); stack.unshift(proxy); changed(); };
    api.moveToEnd = () => { attached(); stack.splice(stack.indexOf(proxy), 1); stack.push(proxy); changed(); };
    api.sourceRectAtTime = (t, includeExtents) => {
      let rect;
      if (type === "TextLayer") {
        const doc = groups[3].property("ADBE Text Document").valueAtTime(t, false);
        rect = { width: 0.6 * doc.fontSize * doc.text.length, height: doc.fontSize, left: 0, top: -0.8 * doc.fontSize };
      } else rect = { width: source && records.get(source).values.isModel ? 200 : source?.width || 0,
        height: source && records.get(source).values.isModel ? 200 : source?.height || 0, left: 0, top: 0 };
      return host("SourceRect", rect);
    };
    const proxy = host(type, api, { values, groups, source });
    return proxy;
  }

  const projectAPI = {};
  projectAPI.items = itemCollection();
  projectAPI.importFile = (options) => {
    if (records.get(options)?.type !== "ImportOptions") throw Error("fake AE: expected ImportOptions");
    const file = options.file;
    // ponytail: fixed imported dimensions; add metadata decoding when asset-size tests need it.
    const footage = item("FootageItem", { name: file.name, width: 1920, height: 1080,
      mainSource: { file: file.fsName }, isModel: /\.glb$/i.test(file.name) });
    items.push(footage); changed(); return footage;
  };
  const projectFile = state.project?.file ? File(state.project.file) : null;
  field(projectAPI, "file", () => projectFile);
  const project = host("Project", projectAPI);
  const initialItems = state.project?.items || [];
  for (const saved of initialItems) {
    if (!constructors[saved.type]) throw Error(`fake AE: unsupported state item ${saved.type}`);
    items.push(item(saved.type, saved));
  }
  initialItems.forEach((saved, i) => {
    const current = items[i];
    records.get(current).values.parentFolder = saved.parentFolder ? items[saved.parentFolder - 1] : root;
    if (saved.type === "CompItem") {
      for (const l of saved.layers || []) {
        if (!["AVLayer", "TextLayer"].includes(l.type)) throw Error(`fake AE: unsupported state layer ${l.type}`);
        records.get(current).layers.push(layer(current, l.type, l, l.source ? items[l.source - 1] : null));
      }
    }
  });
  const appAPI = {
    version: state.app?.version ?? "24.6.0x45", project,
    fonts: host("Fonts", { allFonts: fonts.map((family) => family.map((font) => host("Font", {
      familyName: font.familyName ?? "", styleName: font.styleName ?? "", postScriptName: font.postScriptName ?? "",
    }))) }),
    beginUndoGroup(name) { counters.undoGroups++; },
    endUndoGroup() {},
  };
  const sandbox = { app: host("Application", appAPI),
    File: host("File", File), Folder: host("Folder", Folder), ImportOptions: host("ImportOptions", ImportOptions),
    CompItem: host("CompItem", CompItem), FootageItem: host("FootageItem", FootageItem), FolderItem: host("FolderItem", FolderItem),
    AVLayer: host("AVLayer", AVLayer), TextLayer: host("TextLayer", TextLayer),
    TextDocument: host("TextDocument", TextDocument), KeyframeEase: host("KeyframeEase", KeyframeEase),
    KeyframeInterpolationType, ParagraphJustification, PropertyValueType };
  const dollar = { getenv: (name) => Object.hasOwn(env, name) ? env[name] : null, line: 0 };
  sandbox.$ = host("$", dollar);
  const context = vm.createContext(sandbox);
  dollar.global = vm.runInContext("this", context);
  vm.runInContext("delete JSON;", context);

  function serialize() {
    const reference = (object) => object === root || !object ? null : items.indexOf(object) + 1;
    return { app: { version: appAPI.version, fonts: copy(fonts), env: copy(env) },
      project: { file: projectFile?.fsName || null,
        rootFolder: { name: root.name, comment: root.comment }, items: items.map((current) => {
        const r = records.get(current), v = r.values;
        const result = { type: r.type, name: v.name, comment: v.comment, parentFolder: reference(v.parentFolder) };
        if (r.type === "FootageItem") Object.assign(result, { width: v.width, height: v.height, isModel: v.isModel,
          mainSource: { ...v.mainSource, file: v.mainSource.file?.fsName || null,
            ...(v.mainSource.color ? { color: copy(v.mainSource.color) } : {}) } });
        if (r.type === "CompItem") Object.assign(result, { width: v.width, height: v.height, pixelAspect: v.pixelAspect,
          frameRate: v.frameRate, duration: v.duration, bgColor: copy(v.bgColor), layers: r.layers.map((l) => {
            const lr = records.get(l), lv = lr.values;
            return { type: lr.type, name: lv.name, comment: lv.comment, label: lv.label, inPoint: lv.inPoint,
              outPoint: lv.outPoint, startTime: lv.startTime, threeDLayer: lv.threeDLayer, source: reference(lr.source),
              properties: lr.groups.map(serializeProperty) };
          }) });
        return result;
      }) } };
  }
  return { context, serialize, counters };
}

module.exports = { createAE };
