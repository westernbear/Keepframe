"use strict";
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

// Keep implementation data outside proxies: JSX only sees the documented surface.
const records = new WeakMap();
const copy = (value) => JSON.parse(JSON.stringify(value));
function field(target, name, get, set) {
  Object.defineProperty(target, name, { configurable: true, enumerable: true, get, set });
}

function createAE({ state = {}, documents = process.cwd() } = {}) {
  const context = vm.createContext({});
  const realm = vm.runInContext("({Array: Array, Object: Object, Error: Error, Function: Function})", context);
  const Error = realm.Error;
  // Delete in the VM only; implementation code keeps using host built-ins.
  const removed = [
    ["Array.prototype", "indexOf lastIndexOf forEach map filter reduce reduceRight some every"],
    ["Array", "isArray"], ["Object", "keys create defineProperty defineProperties getPrototypeOf freeze assign entries values"],
    ["String.prototype", "trim trimStart trimEnd startsWith endsWith includes padStart padEnd repeat"],
    ["Function.prototype", "bind"], ["Date", "now"], ["Number", "isFinite isNaN"],
    ["this", "JSON Promise Map Set Symbol Proxy Reflect"],
  ];
  vm.runInContext(removed.flatMap(([object, names]) => names.split(" ").map((name) => `delete ${object}.${name};`)).join("\n"), context);
  const contextGlobal = vm.runInContext("this", context);
  const functions = new WeakMap();
  const canonical = (value) => records.get(value)?.canonical || value;
  const unsupported = (type, name) => { throw Error(`fake AE: unsupported ${type}.${String(name)}`); };
  function scriptValue(value) {
    if (records.has(value)) {
      const r = records.get(value);
      if (["CompItem", "FootageItem", "FolderItem", "AVLayer", "TextLayer"].includes(r.type) && typeof r.target !== "function") {
        return host(r.type, r.target, { ...r, canonical: canonical(value) });
      }
      return value;
    }
    if (value === null || (typeof value !== "object" && typeof value !== "function") || value === contextGlobal) return value;
    if (typeof value === "function") return functions.get(value) || host("Function", value);
    const result = Array.isArray(value) ? new realm.Array(value.length) : new realm.Object();
    for (const name of Object.keys(value)) Object.defineProperty(result, name, {
      value: scriptValue(value[name]), writable: true, enumerable: true, configurable: true,
    });
    return result;
  }
  function boundary(operation) {
    try { return operation(); }
    catch (error) {
      if (!(error instanceof globalThis.Error)) throw error;
      const converted = Error(error.message);
      converted.name = error.name; converted.stack = error.stack;
      throw converted;
    }
  }
  function host(type, target, record = {}, indexed) {
    if (typeof target === "function") Object.defineProperty(target, Symbol.hasInstance, {
      configurable: true,
      value: (value) => Function.prototype[Symbol.hasInstance].call(target, records.get(value)?.target || value),
    });
    const proxy = new Proxy(target, {
      get(object, name) { return boundary(() => {
        if (Object.hasOwn(object, name)) return scriptValue(Reflect.get(object, name));
        if (indexed && typeof name === "string" && /^[1-9]\d*$/.test(name)) {
          const value = indexed(Number(name));
          if (value !== undefined) return scriptValue(value);
        }
        return unsupported(type, name);
      }); },
      set(object, name, value) { return boundary(() => {
        const descriptor = Object.getOwnPropertyDescriptor(object, name);
        if (!descriptor) return unsupported(type, name);
        if (!descriptor.set) throw Error(`fake AE: read-only ${type}.${String(name)}`);
        descriptor.set(value);
        return true;
      }); },
      apply(fn, receiver, args) { return boundary(() => scriptValue(Reflect.apply(fn, receiver, args))); },
      construct(fn, args) { return boundary(() => scriptValue(Reflect.construct(fn, args))); },
      getPrototypeOf() { return typeof target === "function" ? realm.Function.prototype : realm.Object.prototype; },
      getOwnPropertyDescriptor(object, name) { return boundary(() => {
        const descriptor = Object.getOwnPropertyDescriptor(object, name);
        if (!descriptor) return undefined;
        for (const key of ["value", "get", "set"]) if (key in descriptor) descriptor[key] = scriptValue(descriptor[key]);
        return descriptor;
      }); },
      defineProperty(_object, name) { return unsupported(type, name); },
      deleteProperty(_object, name) { return unsupported(type, name); },
      setPrototypeOf() { return unsupported(type, "__proto__"); },
      preventExtensions() { return unsupported(type, "preventExtensions"); },
    });
    records.set(proxy, { type, target, ...record });
    if (typeof target === "function") functions.set(target, proxy);
    return proxy;
  }
  function finite(value, name) {
    if (typeof value !== "number" || !Number.isFinite(value)) throw Error(`fake AE: invalid ${name}`);
  }
  function vector(value, size, name, color = false) {
    if (!Array.isArray(value) || value.length !== size) throw Error(`fake AE: invalid ${name} dimensions`);
    for (let i = 0; i < value.length; i++) {
      finite(value[i], name);
      if (color && (value[i] < 0 || value[i] > 1)) throw Error(`fake AE: invalid ${name} range`);
    }
  }
  const counters = { undoGroups: 0, writes: 0 };
  const calls = [];
  const testHooks = copy(state.testHooks || {});
  const fault = (operation, matchName) => {
    if (testHooks[`${operation}Property`] === matchName) throw Error(`fake AE: injected ${operation} ${matchName}`);
  };
  const changed = () => { counters.writes++; };
  const env = copy(state.app?.env ?? state.env ?? {});
  const fonts = copy(state.app?.fonts ?? state.fonts ?? []);
  function setting(target, values, name, validate = () => {}, count = true, store = (v) => v) {
    field(target, name, () => Array.isArray(values[name]) ? copy(values[name]) : values[name], (value) => {
      validate(value);
      values[name] = store(Array.isArray(value) ? copy(value) : value);
      if (count) changed();
    });
  }
  const enumObject = (type, names) => host(type, Object.fromEntries(names.map((name) => [name, name])));
  const KeyframeInterpolationType = enumObject("KeyframeInterpolationType", ["LINEAR", "BEZIER", "HOLD"]);
  const ParagraphJustification = enumObject("ParagraphJustification", ["LEFT_JUSTIFY", "CENTER_JUSTIFY", "RIGHT_JUSTIFY"]);
  const PropertyValueType = enumObject("PropertyValueType", ["NO_VALUE", "OneD", "TwoD", "TwoD_SPATIAL",
    "ThreeD", "ThreeD_SPATIAL", "COLOR", "TEXT_DOCUMENT"]);
  const PropertyType = enumObject("PropertyType", ["PROPERTY", "INDEXED_GROUP", "NAMED_GROUP"]);
  const TrackMatteType = enumObject("TrackMatteType", ["ALPHA"]);

  // File/Folder: ported from 2c1c8d1:tests/ae_panel_host.js, with strict surfaces.
  function File(p) {
    let filename = path.resolve(String(p));
    let mode = null, buffer = "", encoding = "BINARY";
    const api = Object.create(File.prototype);
    field(api, "fsName", () => filename);
    field(api, "name", () => encodeURI(path.basename(filename)));
    field(api, "displayName", () => path.basename(filename));
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
    values.fillColor = values.fillColor.map(Math.fround);
    field(api, "fillColor", () => {
      if (!values.applyFill) throw Error("fake AE: text fill is disabled");
      return copy(values.fillColor);
    }, (v) => {
      if (!values.applyFill) throw Error("fake AE: text fill is disabled");
      vector(v, 3, "fillColor", true); values.fillColor = copy(v).map(Math.fround);
    });
    // These are real TextDocument fields; preserve them when only managed styling changes.
    for (const [name, initial] of Object.entries({ tracking: 0, applyStroke: false, strokeColor: [0, 0, 0] })) {
      field(api, name, () => copy(values[name] ?? initial), (v) => {
        if (name === "tracking") finite(v, name);
        if (name === "applyStroke" && typeof v !== "boolean") throw Error("fake AE: invalid applyStroke");
        if (name === "strokeColor") vector(v, 3, name, true);
        values[name] = copy(v);
      });
    }
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
    const data = saved ? copy(saved) : { matchName, name, value: copy(value), keys: [],
      expression: "", expressionEnabled: false, propertyValueType: valueType || (Array.isArray(value) ? value.length === 3 ? "ThreeD" : "TwoD" : "OneD") };
    const avVector = ["ADBE Anchor Point", "ADBE Position", "ADBE Scale"].includes(matchName);
    const pad = (v) => avVector && Array.isArray(v) && v.length === 2 ? [...v, matchName === "ADBE Scale" ? 100 : 0] : v;
    if (avVector) {
      data.propertyValueType = valueType;
      data.value = pad(data.value);
      data.keys.forEach((k) => {
        k.value = pad(k.value);
        for (const side of ["inEases", "outEases"]) {
          if (matchName === "ADBE Scale" && k[side].length === 2) k[side].push(copy(k[side][0]));
        }
      });
    }
    const easeDimensions = () => /_SPATIAL$/.test(data.propertyValueType) || !Array.isArray(data.value) ? 1 : data.value.length;
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
      v = pad(v);
      if (Array.isArray(data.value)) {
        vector(v, data.value.length, name);
      }
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
    field(api, "propertyType", () => "PROPERTY");
    field(api, "canSetExpression", () => true);
    field(api, "expression", () => data.expression, (v) => {
      if (typeof v !== "string") throw Error("fake AE: expression must be a string");
      data.expression = v; data.expressionEnabled = v !== ""; changed();
    });
    setting(api, data, "expressionEnabled", (v) => { if (typeof v !== "boolean") throw Error("fake AE: invalid expressionEnabled"); });
    api.setValue = (v) => {
      fault("write", matchName);
      if (data.keys.length) throw Error("fake AE: setValue on a keyframed property");
      data.value = encode(v); changed();
    };
    api.setValueAtTime = (t, v) => {
      fault("write", matchName);
      finite(t, "key time");
      const encoded = encode(v), existing = data.keys.find((k) => k.time === t);
      if (existing) existing.value = encoded;
      else {
        const ease = () => Array.from({ length: easeDimensions() }, () => ({ speed: 0, influence: 33.33333333333333 }));
        data.keys.push({ time: t, value: encoded, inInterpolation: "LINEAR", outInterpolation: "LINEAR", inEases: ease(), outEases: ease() });
        data.keys.sort((a, b) => a.time - b.time);
      }
      changed();
    };
    api.keyTime = (i) => key(i).time;
    api.keyValue = (i) => { fault("read", matchName); return decode(key(i).value); };
    api.removeKey = (i) => { const removed = key(i); data.value = copy(removed.value); data.keys.splice(i - 1, 1); changed(); };
    api.nearestKeyIndex = (t) => {
      finite(t, "key time");
      if (!data.keys.length) throw Error("fake AE: nearestKeyIndex on an unkeyed property");
      let nearest = 0;
      data.keys.forEach((k, i) => { if (Math.abs(k.time - t) < Math.abs(data.keys[nearest].time - t)) nearest = i; });
      return nearest + 1;
    };
    api.valueAtTime = (t, preExpression) => {
      fault("read", matchName);
      if (data.expressionEnabled && !preExpression) return unsupported("Property", "expression evaluation");
      return decode(sample(t));
    };
    api.setInterpolationTypeAtKey = (i, incoming, outgoing = incoming) => {
      const k = key(i);
      for (const v of [incoming, outgoing]) if (!["LINEAR", "BEZIER", "HOLD"].includes(v)) throw Error("fake AE: invalid interpolation type");
      k.inInterpolation = incoming; k.outInterpolation = outgoing; changed();
      calls.push({operation: "interpolation", matchName, key: i});
    };
    api.keyInInterpolationType = (i) => key(i).inInterpolation;
    api.keyOutInterpolationType = (i) => key(i).outInterpolation;
    api.setTemporalEaseAtKey = (i, incoming, outgoing = incoming) => {
      const k = key(i);
      const encodeEases = (eases) => {
        if (!Array.isArray(eases) || eases.length !== easeDimensions()) throw Error("fake AE: temporal ease dimensions must match property dimensions");
        return Array.prototype.map.call(eases, (e) => {
          if (records.get(e)?.type !== "KeyframeEase") throw Error("fake AE: expected KeyframeEase");
          return copy(records.get(e).values);
        });
      };
      const inEases = encodeEases(incoming), outEases = encodeEases(outgoing);
      k.inEases = inEases; k.outEases = outEases; changed();
      if (k.inInterpolation === "LINEAR") k.inInterpolation = "BEZIER";
      if (k.outInterpolation === "LINEAR") k.outInterpolation = "BEZIER";
      calls.push({operation: "ease", matchName, key: i});
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
    field(api, "propertyType", () => additions ? "INDEXED_GROUP" : "NAMED_GROUP");
    if (effectDefinitions[matchName]) {
      values.enabled = saved?.enabled ?? true;
      setting(api, values, "enabled", (v) => { if (typeof v !== "boolean") throw Error("fake AE: invalid enabled"); });
    }
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
    if (effectDefinitions[matchName] && !children.some((p) => p.matchName === "ADBE Effect Built In Params")) {
      children.push(group("ADBE Effect Built In Params", "Compositing Options", [
        property("ADBE Effect Mask Opacity", "Effect Opacity", 100),
      ]));
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
    return { matchName: r.matchName, name: r.values.name,
      ...(r.values.enabled !== undefined ? {enabled: r.values.enabled} : {}), properties: r.children.map(serializeProperty) };
  }

  // Layer transforms, including scalar followers for separated position.
  function transformGroup(layerValues, comp, source, saved) {
    const definitions = [
      ["ADBE Anchor Point", "Anchor Point", source ? [source.width / 2, source.height / 2, 0] : [0, 0, 0], "ThreeD_SPATIAL"],
      ["ADBE Position", "Position", [comp.width / 2, comp.height / 2, 0], "ThreeD_SPATIAL"],
      ["ADBE Scale", "Scale", [100, 100, 100], "ThreeD"], ["ADBE Rotate Z", "Rotation", 0],
      ["ADBE Opacity", "Opacity", 100], ["ADBE Rotate X", "X Rotation", 0],
      ["ADBE Rotate Y", "Y Rotation", 0], ["ADBE Orientation", "Orientation", [0, 0, 0], "ThreeD"],
      ["ADBE Position_0", "X Position", comp.width / 2], ["ADBE Position_1", "Y Position", comp.height / 2],
      ["ADBE Position_2", "Z Position", 0],
    ];
    const children = definitions.map(([match, name, value, valueType]) => {
      const old = saved?.properties.find((p) => p.matchName === match);
      return property(match, name, value, valueType, old);
    });
    const g = group("ADBE Transform Group", saved?.name || "Transform", children);
    const position = children[1], r = records.get(position);
    const followers = () => children.slice(8, 11);
    r.data.dimensionsSeparated ??= false;
    const sample = r.api.valueAtTime, setValue = r.api.setValue, setValueAtTime = r.api.setValueAtTime;
    const visible = () => children.filter((p, i) => (i < 5 || (i < 8 && layerValues.threeDLayer)
      || (i >= 8 && r.data.dimensionsSeparated)));
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
          followers().forEach((p, dimension) => {
            const follower = records.get(p);
            follower.data.value = r.data.value[dimension];
            follower.data.keys = r.data.keys.map((k) => ({ ...copy(k), value: k.value[dimension],
              inEases: [copy(k.inEases[0])], outEases: [copy(k.outEases[0])] }));
          });
          r.data.keys = [];
        } else {
          const active = followers(), data = active.map((p) => records.get(p).data);
          r.data.value = data.map((d) => d.value);
          const times = [...new Set(data.flatMap((d) => d.keys.map((k) => k.time)))].sort((a, b) => a - b);
          r.data.keys = times.map((t) => {
            const base = data.map((d) => d.keys.find((k) => k.time === t)).find(Boolean);
            return { ...copy(base), value: active.map((p) => p.valueAtTime(t, true)),
              inEases: [copy(base.inEases[0])], outEases: [copy(base.outEases[0])] };
          });
        }
        r.data.dimensionsSeparated = value;
      }
      changed();
    });
    r.api.valueAtTime = (t, pre) => r.data.dimensionsSeparated ? followers().map((p) => p.valueAtTime(t, pre)) : sample(t, pre);
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
  let nextId = Math.max(0, ...(state.project?.items || []).map((v) => v.id || 0)) + 1;
  const ids = new Set();
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
    return collection("ItemCollection", () => parent ? items.filter((child) => canonical(child.parentFolder) === canonical(parent)) : items, {
      addComp(name, width, height, pixelAspect, duration, frameRate) {
        return add("CompItem", { name, width, height, pixelAspect, duration, frameRate });
      },
      addFolder(name) { return add("FolderItem", { name }); },
    });
  }
  function item(type, saved) {
    const values = { name: "", comment: "", ...copy(saved), parentFolder: root || null };
    const api = Object.create(constructors[type].prototype);
    values.id = saved.id && !ids.has(saved.id) ? saved.id : nextId++;
    ids.add(values.id);
    nextId = Math.max(nextId, values.id + 1);
    field(api, "id", () => values.id);
    setting(api, values, "name"); setting(api, values, "comment");
    field(api, "parentFolder", () => values.parentFolder, (folder) => {
      if (records.get(folder)?.type !== "FolderItem") throw Error("fake AE: parentFolder must be a FolderItem");
      for (let ancestor = folder; ancestor; ancestor = ancestor.parentFolder) {
        if (canonical(ancestor) === proxy) throw Error("fake AE: cyclic parentFolder");
      }
      values.parentFolder = canonical(folder); changed();
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
        if (source.color) values.mainSource.color = copy(source.color).map(Math.fround);
        const sourceAPI = {};
        field(sourceAPI, "file", () => values.mainSource.file);
        if (source.color) field(sourceAPI, "color", () => copy(values.mainSource.color));
        api.mainSource = host(source.color ? "SolidSource" : "FileSource", sourceAPI);
        field(api, "file", () => values.mainSource.file);
        api.replace = (file) => {
          if (records.get(file)?.type !== "File") throw Error("fake AE: replace requires a File");
          values.mainSource.file = file;
          values.isModel = /\.glb$/i.test(file.name);
          changed();
        };
      } else {
        values.frameRate ??= 30; values.duration ??= 5; values.pixelAspect ??= 1; values.bgColor ??= [0, 0, 0];
        values.renderer ??= "ADBE Advanced 3d";
        values.renderers ??= state.renderers || ["ADBE Advanced 3d", "ADBE Ernst", "ADBE Calder"];
        field(api, "renderers", () => copy(values.renderers));
        setting(api, values, "renderer", (v) => {
          if (!values.renderers.includes(v)) throw Error("fake AE: invalid renderer");
        });
        for (const name of ["frameRate", "duration", "pixelAspect"]) {
          const store = name === "pixelAspect" ? (v) => v : Math.fround;
          values[name] = store(values[name]);
          setting(api, values, name, (v) => {
            finite(v, name); if (v <= 0) throw Error(`fake AE: invalid ${name}`);
          }, true, store);
        }
        values.bgColor = values.bgColor.map(Math.fround);
        setting(api, values, "bgColor", (v) => vector(v, 3, "bgColor", true), true, (v) => v.map(Math.fround));
        const layers = [];
        records.get(proxy).layers = layers;
        const addLayer = (layerType, sourceItem, name, duration, text, nullLayer = false) => {
          const l = layer(proxy, layerType, { name, outPoint: duration ?? proxy.duration, text, nullLayer }, canonical(sourceItem));
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
          addNull(duration) { return addLayer("AVLayer", solid([1, 1, 1], "Null", 100, 100), "Null", duration, undefined, true); },
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
    values.parent = null; values.trackMatteLayer = null;
    const api = Object.create((type === "TextLayer" ? TextLayer : AVLayer).prototype);
    for (const name of ["name", "comment"]) setting(api, values, name);
    setting(api, values, "label", (v) => { if (!Number.isInteger(v) || v < 0 || v > 16) throw Error("fake AE: label must be 0–16"); });
    for (const name of ["inPoint", "outPoint", "startTime"]) setting(api, values, name, (v) => finite(v, name));
    const stack = records.get(comp).layers;
    field(api, "index", () => stack.indexOf(proxy) + 1);
    field(api, "source", () => source || null);
    field(api, "parent", () => values.parent, (v) => {
      v = canonical(v);
      if (v && !stack.includes(v)) throw Error("fake AE: parent must be in the same comp");
      for (let ancestor = v; ancestor; ancestor = canonical(ancestor.parent)) {
        if (ancestor === proxy) throw Error("fake AE: cyclic parent");
      }
      values.parent = v; changed();
    });
    field(api, "trackMatteLayer", () => values.trackMatteLayer);
    api.setTrackMatte = (v, matteType) => {
      v = canonical(v);
      if (v === proxy || !stack.includes(v) || matteType !== "ALPHA") throw Error("fake AE: invalid track matte");
      values.trackMatteLayer = v; changed();
    };
    field(api, "nullLayer", () => Boolean(values.nullLayer));
    api.replaceSource = (sourceItem, fixExpressions) => {
      if (type === "TextLayer" || fixExpressions !== false) return unsupported(type, "replaceSource");
      if (!["FootageItem", "CompItem"].includes(records.get(sourceItem)?.type)) throw Error("fake AE: replaceSource requires footage or comp");
      source = canonical(sourceItem);
      records.get(proxy).source = source;
      calls.push({operation: "replaceSource", index: api.index});
      changed();
    };
    const findGroup = (match) => saved.properties?.find((p) => p.matchName === match);
    const groups = [transformGroup(values, comp, source, findGroup("ADBE Transform Group")),
      group("ADBE Effect Parade", "Effects", [], Object.keys(effectDefinitions), findGroup("ADBE Effect Parade")),
      group("ADBE Mask Parade", "Masks", [], ["ADBE Mask Atom"], findGroup("ADBE Mask Parade"))];
    field(api, "threeDLayer", () => values.threeDLayer, (v) => {
      if (typeof v !== "boolean") throw Error("fake AE: invalid threeDLayer");
      values.threeDLayer = v; changed();
    });
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
      target = canonical(target);
      if (!stack.includes(target)) throw Error("fake AE: move target must be in the same comp");
      if (target === proxy) throw Error("fake AE: cannot move a layer before or after itself");
      calls.push({operation: offset ? "moveAfter" : "moveBefore", name: values.name, comment: values.comment, target: target.name});
      stack.splice(stack.indexOf(proxy), 1); stack.splice(stack.indexOf(target) + offset, 0, proxy);
      changed();
    };
    api.remove = () => {
      attached();
      for (const l of stack) {
        const v = records.get(l).values;
        if (v.parent === proxy) v.parent = null;
        if (v.trackMatteLayer === proxy) v.trackMatteLayer = null;
      }
      stack.splice(stack.indexOf(proxy), 1); changed();
    };
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
      return host("SourceRect", scriptValue(rect));
    };
    const proxy = host(type, api, { values, groups, source });
    return proxy;
  }

  const projectAPI = {};
  field(projectAPI, "rootFolder", () => root);
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
      (saved.layers || []).forEach((l, j) => {
        const stack = records.get(current).layers, v = records.get(stack[j]).values;
        v.parent = l.parent ? stack[l.parent - 1] : null;
        v.trackMatteLayer = l.trackMatteLayer ? stack[l.trackMatteLayer - 1] : null;
      });
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
    KeyframeInterpolationType, ParagraphJustification, PropertyValueType, PropertyType, TrackMatteType };
  const dollar = { getenv: (name) => Object.hasOwn(env, name) ? env[name] : null, line: 0 };
  sandbox.$ = host("$", dollar);
  Object.assign(context, sandbox);
  dollar.global = contextGlobal;

  function serialize() {
    const reference = (object) => canonical(object) === root || !object ? null : items.indexOf(canonical(object)) + 1;
    return { app: { version: appAPI.version, fonts: copy(fonts), env: copy(env) },
      ...(Object.keys(testHooks).length ? {testHooks: copy(testHooks)} : {}),
      project: { file: projectFile?.fsName || null,
        rootFolder: { id: root.id, name: root.name, comment: root.comment }, items: items.map((current) => {
        const r = records.get(current), v = r.values;
        const result = { type: r.type, id: v.id, name: v.name, comment: v.comment, parentFolder: reference(v.parentFolder) };
        if (r.type === "FootageItem") Object.assign(result, { width: v.width, height: v.height, isModel: v.isModel,
          mainSource: { ...v.mainSource, file: v.mainSource.file?.fsName || null,
            ...(v.mainSource.color ? { color: copy(v.mainSource.color) } : {}) } });
        if (r.type === "CompItem") Object.assign(result, { width: v.width, height: v.height, pixelAspect: v.pixelAspect,
          frameRate: v.frameRate, duration: v.duration, bgColor: copy(v.bgColor), renderer: v.renderer, renderers: copy(v.renderers), layers: r.layers.map((l) => {
            const lr = records.get(l), lv = lr.values;
            return { type: lr.type, name: lv.name, comment: lv.comment, label: lv.label, inPoint: lv.inPoint,
              outPoint: lv.outPoint, startTime: lv.startTime, threeDLayer: lv.threeDLayer, nullLayer: Boolean(lv.nullLayer), source: reference(lr.source),
              ...(lv.parent ? {parent: r.layers.indexOf(lv.parent) + 1} : {}),
              ...(lv.trackMatteLayer ? {trackMatteLayer: r.layers.indexOf(lv.trackMatteLayer) + 1} : {}),
              properties: lr.groups.map(serializeProperty) };
          }) });
        return result;
      }) } };
  }
  return { context, serialize, counters, calls };
}

module.exports = { createAE };
