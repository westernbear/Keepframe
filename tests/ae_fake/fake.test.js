"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { spawnSync } = require("node:child_process");
const { createAE } = require("./ae");

function fixture(options) {
  const ae = createAE(options);
  const comp = ae.context.app.project.items.addComp("Test", 640, 480, 1, 5, 30);
  const layer = comp.layers.addText("Hello");
  const transform = layer.property("ADBE Transform Group");
  return { ...ae, comp, layer, transform, opacity: transform.property("ADBE Opacity") };
}

test("VM has no JSON, shares $.global, and exposes state/version/fonts/env", () => {
  const ae = createAE({ state: { app: { version: "25.0", fonts: [[{
    familyName: "Arial", styleName: "Regular", postScriptName: "ArialMT",
  }]], env: { HOME: "/example" } } } });
  assert.equal(vm.runInContext("typeof JSON", ae.context), "undefined");
  assert.equal(vm.runInContext("$.global === this", ae.context), true);
  assert.equal(ae.context.app.version, "25.0");
  assert.equal(ae.context.app.fonts.allFonts[0][0].postScriptName, "ArialMT");
  assert.equal(ae.context.$.getenv("HOME"), "/example");
  assert.equal(ae.context.$.getenv("MISSING"), null);
  assert.equal(ae.context.$.getenv("toString"), null);
  assert.equal(ae.context.$.line, 0);
  assert.equal(ae.context.app.project.file, null);
});

test("unknown properties, methods, writes, and property lookups fail loudly", () => {
  const f = fixture();
  const effect = f.layer.Effects.addProperty("ADBE Linear Wipe");
  const objects = [
    [f.context.app, "Application"], [f.context.app.project, "Project"],
    [f.context.app.project.items, "ItemCollection"], [f.comp, "CompItem"],
    [f.comp.layers, "LayerCollection"], [f.layer, "TextLayer"],
    [f.transform, "PropertyGroup"], [effect, "PropertyGroup"],
    [f.opacity, "Property"], [new f.context.TextDocument("x"), "TextDocument"],
    [new f.context.KeyframeEase(0, 33), "KeyframeEase"],
    [new f.context.File("x"), "File"], [new f.context.Folder("x"), "Folder"],
    [new f.context.ImportOptions(new f.context.File("x")), "ImportOptions"],
    [f.context.$, "$"], [f.context.app.fonts, "Fonts"],
    [f.context.KeyframeInterpolationType, "KeyframeInterpolationType"],
    [f.context.ParagraphJustification, "ParagraphJustification"],
    [f.context.PropertyValueType, "PropertyValueType"],
    [f.layer.sourceRectAtTime(0, false), "SourceRect"],
  ];
  for (const [object, type] of objects) {
    const message = `fake AE: unsupported ${type}.bogus`;
    assert.throws(() => object.bogus, { message });
    assert.throws(() => object.bogus(), { message });
    assert.throws(() => { object.bogus = 1; }, { message });
  }
  assert.throws(() => f.layer.property("bad"), /unsupported TextLayer.bad/);
  assert.throws(() => f.transform.property("bad"), /unsupported PropertyGroup.bad/);
  assert.throws(() => f.layer.Effects.addProperty("bad"), /unsupported PropertyGroup.bad/);
  assert.throws(() => f.context.app.project.renderQueue, /unsupported Project.renderQueue/);
  assert.equal(f.layer.comment, "");
  assert.equal(f.layer.source, null);
  assert.equal(f.opacity.expression, "");
  assert.equal(f.opacity.expressionEnabled, false);
  const withFont = createAE({ state: { app: { fonts: [[{ familyName: "Test" }]] } } });
  assert.equal(withFont.context.app.fonts.allFonts[0][0].styleName, "");
  assert.throws(() => withFont.context.app.fonts.allFonts[0][0].bogus, /unsupported Font.bogus/);
});

test("keys replace, sort, interpolate, hold outside range, and remove", () => {
  const { opacity: p, transform, context } = fixture();
  p.setValueAtTime(2, 100);
  p.setValueAtTime(0, 0);
  p.setValueAtTime(2, 80);
  assert.equal(p.numKeys, 2);
  assert.equal(p.keyTime(1), 0);
  assert.equal(p.keyValue(2), 80);
  assert.equal(p.valueAtTime(1, false), 40);
  assert.equal(p.valueAtTime(-1, false), 0);
  assert.equal(p.valueAtTime(3, false), 80);
  assert.equal(p.nearestKeyIndex(1.9), 2);
  assert.throws(() => p.setValue(50), { message: "fake AE: setValue on a keyframed property" });
  p.setInterpolationTypeAtKey(1, context.KeyframeInterpolationType.HOLD,
    context.KeyframeInterpolationType.HOLD);
  assert.equal(p.keyInInterpolationType(1), context.KeyframeInterpolationType.HOLD);
  assert.equal(p.keyOutInterpolationType(1), context.KeyframeInterpolationType.HOLD);
  assert.equal(p.valueAtTime(1, false), 0);
  p.removeKey(1);
  assert.equal(p.keyTime(1), 2);
  p.removeKey(1);
  p.setValue(25);
  assert.equal(p.value, 25);
  assert.throws(() => p.keyValue(0), /key index/);
  assert.throws(() => p.removeKey(1), /key index/);
  const position = transform.property("ADBE Position");
  position.setValueAtTime(0, [0, 10]);
  position.setValueAtTime(2, [20, 30]);
  assert.deepEqual(position.valueAtTime(1, true), [10, 20]);
  const copy = position.keyValue(1);
  copy[0] = 999;
  assert.deepEqual(position.keyValue(1), [0, 10]);
});

test("ease dimension checks, interpolation, and influence boundaries", () => {
  const { context, transform, opacity } = fixture();
  const ease = new context.KeyframeEase(4, 33);
  new context.KeyframeEase(0, 0.1);
  new context.KeyframeEase(0, 100);
  for (const influence of [0, 100.1, NaN]) {
    assert.throws(() => new context.KeyframeEase(0, influence), /influence/);
  }
  assert.throws(() => { ease.influence = 0; }, /influence/);
  const p = transform.property("ADBE Scale");
  p.setValueAtTime(0, [100, 100]);
  assert.throws(() => p.setTemporalEaseAtKey(1, [ease], [ease]), /dimensions/);
  p.setTemporalEaseAtKey(1, [ease, ease], [ease, ease]);
  assert.equal(p.keyInTemporalEase(1)[0].speed, 4);
  assert.equal(p.keyOutTemporalEase(1)[1].influence, 33);
  ease.speed = 99;
  assert.equal(p.keyInTemporalEase(1)[0].speed, 4);
  p.setInterpolationTypeAtKey(1, context.KeyframeInterpolationType.BEZIER,
    context.KeyframeInterpolationType.LINEAR);
  assert.equal(p.keyInInterpolationType(1), context.KeyframeInterpolationType.BEZIER);
  opacity.setValueAtTime(0, 100);
  opacity.setTemporalEaseAtKey(1, [ease], [ease]);
  assert.equal(opacity.keyOutTemporalEase(1).length, 1);
  assert.equal(p.propertyValueType, context.PropertyValueType.TwoD);
  assert.throws(() => p.setValueAtTime(1, [100]), /dimensions/);
});

test("separated position exposes scalar X/Y and survives toggling", () => {
  const { transform } = fixture();
  const p = transform.property("ADBE Position");
  p.setValue([10, 20]);
  p.dimensionsSeparated = true;
  const x = transform.property("ADBE Position_0");
  const y = transform.property("ADBE Position_1");
  assert.equal(x.value, 10);
  assert.equal(y.value, 20);
  x.setValueAtTime(0, 10);
  x.setValueAtTime(2, 30);
  y.setValue(40);
  assert.equal(x.valueAtTime(1, false), 20);
  assert.deepEqual(p.valueAtTime(1, false), [20, 40]);
  p.dimensionsSeparated = false;
  assert.deepEqual(p.valueAtTime(1, false), [20, 40]);
  assert.throws(() => transform.property("ADBE Position_0"), /unsupported/);
  p.dimensionsSeparated = true;
  assert.equal(transform.property("ADBE Position_0").valueAtTime(1, false), 20);
  assert.throws(() => p.setValue([0, 0]), /separated position/);
});

test("layers add on top; all moves and removal update indices", () => {
  const { comp, layer: a } = fixture();
  const b = comp.layers.addNull(5);
  const c = comp.layers.addSolid([1, 0, 0], "Solid", 20, 30, 1, 5);
  const order = (expected) => {
    assert.equal(comp.layers.length, expected.length);
    expected.forEach((layer, i) => {
      assert.equal(comp.layers[i + 1], layer);
      assert.equal(layer.index, i + 1);
    });
  };
  order([c, b, a]);
  a.moveBefore(b); order([c, a, b]);
  c.moveAfter(b); order([a, b, c]);
  c.moveToBeginning(); order([c, a, b]);
  c.moveToEnd(); order([a, b, c]);
  b.remove(); order([a, c]);
  assert.throws(() => b.remove(), /removed layer/);
  assert.throws(() => a.moveBefore(b), /same comp/);
  assert.throws(() => comp.layers[0], /unsupported LayerCollection.0/);
});

test("project folders, footage replacement, source rectangles, 3D and text", () => {
  const f = fixture();
  const { context: c } = f;
  const folder = c.app.project.items.addFolder("Keepframe");
  const image = c.app.project.importFile(new c.ImportOptions(new c.File("image.png")));
  image.parentFolder = folder;
  assert.equal(folder.items.length, 1);
  assert.equal(folder.items[1], image);
  assert.ok(image instanceof c.FootageItem);
  assert.ok(folder instanceof c.FolderItem);
  assert.ok(f.comp instanceof c.CompItem);
  const av = f.comp.layers.add(image);
  assert.ok(av instanceof c.AVLayer);
  assert.ok(f.layer instanceof c.TextLayer);
  assert.throws(() => image.bogus, /unsupported FootageItem.bogus/);
  assert.throws(() => image.mainSource.bogus, /unsupported FileSource.bogus/);
  assert.throws(() => folder.bogus, /unsupported FolderItem.bogus/);
  image.replace(new c.File("replacement.png"));
  assert.equal(image.mainSource.file.name, "replacement.png");
  const rect = av.sourceRectAtTime(0, false);
  assert.equal(rect.width, image.width);
  assert.equal(rect.height, image.height);
  const model = c.app.project.importFile(new c.ImportOptions(new c.File("model.glb")));
  const modelLayer = f.comp.layers.add(model);
  assert.equal(modelLayer.threeDLayer, true);
  assert.equal(modelLayer.sourceRectAtTime(0, false).width, 200);
  assert.equal(modelLayer.sourceRectAtTime(0, false).height, 200);
  assert.deepEqual(modelLayer.property("ADBE Transform Group").property("ADBE Orientation").value, [0, 0, 0]);
  const text = f.layer.property("ADBE Text Properties").property("ADBE Text Document");
  const doc = text.value;
  doc.fontSize = 10;
  doc.fillColor = [1, 0.5, 0];
  doc.justification = c.ParagraphJustification.CENTER_JUSTIFY;
  text.setValue(doc);
  doc.text = "not committed";
  assert.equal(text.value.text, "Hello");
  const tr = f.layer.sourceRectAtTime(0, false);
  assert.equal(tr.width, 30);
  assert.equal(tr.height, 10);
  assert.equal(tr.top, -8);
  assert.throws(() => { f.layer.label = 17; }, /label/);
  assert.throws(() => { doc.fillColor = [255, 0, 0]; }, /fillColor/);
});

test("effect match names, enumeration, names and removal; masks", () => {
  const { layer } = fixture();
  assert.equal(layer.Effects, layer.property("ADBE Effect Parade"));
  assert.equal(layer.Masks, layer.property("ADBE Mask Parade"));
  const wipe = layer.Effects.addProperty("ADBE Linear Wipe");
  assert.equal(wipe.matchName, "ADBE Linear Wipe");
  assert.equal(wipe.numProperties, 3);
  for (let i = 1; i <= 3; i++) {
    assert.equal(wipe.property(i).matchName, `ADBE Linear Wipe-000${i}`);
  }
  assert.equal(wipe.property("ADBE Linear Wipe-0001").name, "Transition Completion");
  wipe.name = "Reveal";
  assert.equal(layer.Effects.property("Reveal"), wipe);
  const transform = layer.Effects.addProperty("ADBE Geometry2");
  for (const suffix of ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0011"]) {
    assert.equal(transform.property(`ADBE Geometry2-${suffix}`).matchName, `ADBE Geometry2-${suffix}`);
  }
  wipe.remove();
  assert.equal(layer.Effects.numProperties, 1);
  assert.throws(() => wipe.remove(), /removed property group/);
  const mask = layer.Masks.addProperty("ADBE Mask Atom");
  assert.equal(mask.matchName, "ADBE Mask Atom");
  mask.remove();
  assert.equal(layer.Masks.numProperties, 0);
});

test("counters count every mutating call and assignment, even the same value", () => {
  const f = fixture();
  const { layer, comp, opacity: p, counters, context: c } = f;
  const once = (operation) => {
    const before = counters.writes;
    operation();
    assert.equal(counters.writes, before + 1);
  };
  c.app.beginUndoGroup("sync"); c.app.endUndoGroup();
  assert.equal(counters.undoGroups, 1);
  for (const [object, names] of [[layer, ["name", "comment", "label", "inPoint", "outPoint", "startTime", "threeDLayer"]],
    [comp, ["name", "comment", "width", "height", "frameRate", "duration", "bgColor"]],
    [p, ["expression", "expressionEnabled"]]]) {
    for (const name of names) once(() => { object[name] = object[name]; });
  }
  once(() => { f.transform.property("ADBE Position").dimensionsSeparated = false; });
  once(() => p.setValue(100));
  once(() => p.setValueAtTime(0, 100));
  once(() => p.setValueAtTime(0, 100));
  once(() => p.setTemporalEaseAtKey(1, [new c.KeyframeEase(0, 33)], [new c.KeyframeEase(0, 33)]));
  once(() => p.setInterpolationTypeAtKey(1, c.KeyframeInterpolationType.LINEAR, c.KeyframeInterpolationType.LINEAR));
  once(() => p.removeKey(1));
  let effect;
  once(() => { effect = layer.Effects.addProperty("ADBE Linear Wipe"); });
  once(() => { effect.name = effect.name; });
  once(() => effect.remove());
  once(() => layer.moveToBeginning()); once(() => layer.moveToEnd());
  const other = comp.layers.addNull(5);
  once(() => layer.moveBefore(other)); once(() => layer.moveAfter(other));
  once(() => other.remove());
  once(() => comp.layers.addText("x"));
  once(() => comp.layers.addSolid([0, 0, 0], "S", 20, 20, 1, 5));
  once(() => comp.layers.addNull(5));
  once(() => c.app.project.items.addComp("C", 20, 20, 1, 5, 30));
  once(() => c.app.project.items.addFolder("F"));
  let footage;
  once(() => { footage = c.app.project.importFile(new c.ImportOptions(new c.File("a.png"))); });
  once(() => comp.layers.add(footage));
  once(() => footage.replace(new c.File("b.png")));
  const before = counters.writes;
  p.valueAtTime(0, true); layer.sourceRectAtTime(0, false); f.serialize();
  assert.equal(counters.writes, before);
});

test("serialize -> JSON -> createAE preserves the whole model and source references", () => {
  const f = fixture({ state: { app: { version: "26", env: { X: "Y" }, fonts: [[{
    familyName: "Test", styleName: "Regular", postScriptName: "Test-Regular",
  }]] }, project: { file: "/tmp/example.aep" } } });
  const c = f.context;
  const folder = c.app.project.items.addFolder("Keepframe");
  f.comp.parentFolder = folder;
  f.comp.comment = "comp-tag";
  f.comp.parentFolder.parentFolder.name = "Custom root";
  f.transform.name = "Custom transform";
  f.layer.comment = "layer-tag;fp=123";
  f.layer.threeDLayer = true;
  f.layer.property("ADBE Transform Group").property("ADBE Rotate X").setValue(12);
  f.opacity.setValueAtTime(0, 0); f.opacity.setValueAtTime(2, 100);
  f.opacity.expression = "time * 10"; f.opacity.expressionEnabled = true;
  f.opacity.setTemporalEaseAtKey(1, [new c.KeyframeEase(2, 40)], [new c.KeyframeEase(3, 60)]);
  f.opacity.setInterpolationTypeAtKey(1, c.KeyframeInterpolationType.BEZIER, c.KeyframeInterpolationType.HOLD);
  const position = f.transform.property("ADBE Position");
  position.dimensionsSeparated = true;
  f.transform.property("ADBE Position_0").setValueAtTime(1, 9);
  f.layer.Effects.addProperty("ADBE Geometry2").property("ADBE Geometry2-0008").setValue(50);
  f.layer.Masks.addProperty("ADBE Mask Atom").name = "Mask";
  const doc = new c.TextDocument("Saved"); doc.fontSize = 48;
  f.layer.property("ADBE Text Properties").property("ADBE Text Document").setValueAtTime(0, doc);
  const model = c.app.project.importFile(new c.ImportOptions(new c.File("asset.glb")));
  model.parentFolder = folder; model.comment = "sha256";
  f.comp.layers.add(model);
  f.comp.layers.addSolid([1, 0.5, 0], "Solid", 10, 20, 1, 5);
  f.comp.layers.addNull(5);
  const second = c.app.project.items.addComp("Nested", 10, 10, 1, 5, 24);
  f.comp.layers.add(second);
  const state = JSON.parse(JSON.stringify(f.serialize()));
  const detached = f.serialize();
  detached.project.items[3].mainSource.color[0] = 0;
  assert.equal(f.serialize().project.items[3].mainSource.color[0], 1);
  const restored = createAE({ state });
  assert.deepEqual(restored.serialize(), state);
  assert.deepEqual(restored.counters, { undoGroups: 0, writes: 0 });
  const comp = restored.context.app.project.items[1];
  assert.equal(comp.layers[1].source, restored.context.app.project.items[6]);
  assert.equal(comp.parentFolder.items[1], comp);
  assert.equal(restored.context.app.project.file.name, "example.aep");
  const restoredText = comp.layers[5];
  const effect = restoredText.Effects.property("ADBE Geometry2");
  assert.throws(() => effect.property("ADBE Geometry2-0008").remove, /unsupported Property.remove/);
  effect.remove();
  restoredText.Effects.addProperty("ADBE Linear Wipe");
  assert.equal(restoredText.Effects.numProperties, 1);
  const mask = restoredText.Masks.property(1);
  mask.remove();
  assert.equal(restoredText.Masks.numProperties, 0);
});

test("expressions persist; unsupported expression evaluation fails loudly", () => {
  const { opacity, layer } = fixture();
  opacity.expression = "time * 10";
  assert.equal(opacity.expressionEnabled, true);
  assert.throws(() => opacity.valueAtTime(1, false), /unsupported Property.expression evaluation/);
  assert.throws(() => opacity.value, /unsupported Property.expression evaluation/);
  assert.equal(opacity.valueAtTime(1, true), 100);
  opacity.expression = "";
  assert.equal(opacity.expressionEnabled, false);
  assert.equal(layer.property("Text").property("Source Text").canSetExpression, true);
});

test("FolderItem ItemCollection creates children with one counted write", () => {
  const { context: c, counters, serialize } = createAE();
  const folder = c.app.project.items.addFolder("Parent");
  let before = counters.writes;
  const child = folder.items.addFolder("Child");
  assert.equal(counters.writes, before + 1);
  assert.equal(child.parentFolder, folder);
  before = counters.writes;
  const comp = child.items.addComp("Comp", 10, 20, 1, 5, 30);
  assert.equal(counters.writes, before + 1);
  assert.equal(comp.parentFolder, child);
  assert.equal(folder.items[1], child);
  assert.equal(child.items[1], comp);
  assert.equal(c.app.project.items.length, 3);
  assert.deepEqual(createAE({ state: serialize() }).serialize(), serialize());
});

test("ported File/Folder shims read, write, rename, list and remove", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-files-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const { context: c } = createAE({ documents: dir, state: { app: { env: { LOCALAPPDATA: dir } } } });
  assert.equal(c.Folder.myDocuments.fsName, dir);
  assert.equal(c.Folder.userData.fsName, dir);
  const folder = c.Folder(path.join(dir, "nested"));
  assert.equal(folder.create(), true);
  const file = c.File(path.join(folder.fsName, "a.txt"));
  assert.equal(file.encoding, "BINARY");
  assert.equal(file.open("w"), true); file.write("hi"); file.close();
  assert.equal(file.exists, true);
  assert.equal(file.open("r"), true); assert.equal(file.read(), "hi"); file.close();
  fs.writeFileSync(path.join(folder.fsName, "b.txt"), "existing");
  assert.equal(file.rename("b.txt"), false);
  assert.equal(file.rename("c.txt"), true);
  assert.equal(file.name, "c.txt");
  assert.equal(folder.getFiles().length, 2);
  assert.equal(file.remove(), true);
  assert.equal(file.exists, false);
});

test("CLI persists state, returns only JSON, accepts string args and documents; reports JSX errors", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-cli-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = path.join(dir, "tiny.jsx");
  const state = path.join(dir, "state.json");
  const runner = path.join(__dirname, "run.js");
  fs.writeFileSync(script, 'function sync(name) {\n if (typeof JSON !== "undefined") throw Error("JSON leaked");\n if (!app.project.items.length) app.project.items.addComp(name, 10, 10, 1, 5, 30);\n return app.project.items[1].name + ":" + Folder.myDocuments.name;\n}\nfunction fail() {\n throw Error("failure");\n}\n');
  const run = (entry, arg) => spawnSync(process.execPath, [runner, state, script, entry, arg, "--documents", dir], { encoding: "utf8" });
  const first = run("sync", "literal $`\" arg");
  assert.equal(first.status, 0, first.stdout + first.stderr);
  assert.equal(first.stdout.trim().split("\n").length, 1);
  assert.deepEqual(JSON.parse(first.stdout), { result: `literal $\`" arg:${path.basename(dir)}`, undo_groups: 0, writes: 1 });
  assert.equal(JSON.parse(fs.readFileSync(state, "utf8")).project.items.length, 1);
  const second = run("sync", "ignored");
  assert.equal(second.status, 0, second.stdout + second.stderr);
  assert.equal(JSON.parse(second.stdout).result, JSON.parse(first.stdout).result);
  assert.equal(JSON.parse(second.stdout).writes, 0);
  const saved = fs.readFileSync(state, "utf8");
  const failure = run("fail", "");
  assert.equal(failure.status, 1);
  assert.deepEqual(JSON.parse(failure.stdout), { error: "failure", line: 7 });
  assert.equal(fs.readFileSync(state, "utf8"), saved);
});

test("CLI rejects invalid state and return values and locates host errors in JSX", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-errors-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = path.join(dir, "errors.jsx"), state = path.join(dir, "state.json");
  fs.writeFileSync(script, 'function gap() {\n return app.bogus();\n}\n'
    + 'function bad() { return 123; }\nfunction thrown() { throw null; }\n');
  const run = (entry) => spawnSync(process.execPath, [path.join(__dirname, "run.js"), state, script, entry], { encoding: "utf8" });
  let result = run("gap");
  assert.equal(result.status, 1);
  assert.deepEqual(JSON.parse(result.stdout), { error: "fake AE: unsupported Application.bogus", line: 2 });
  assert.equal(fs.existsSync(state), false);
  result = run("bad");
  assert.equal(result.status, 1);
  assert.match(JSON.parse(result.stdout).error, /return a string/);
  result = run("thrown");
  assert.equal(result.status, 1);
  assert.deepEqual(JSON.parse(result.stdout), { error: "null", line: 0 });
  fs.writeFileSync(state, "invalid JSON");
  result = run("bad");
  assert.equal(result.status, 1);
  assert.equal(typeof JSON.parse(result.stdout).error, "string");
  assert.equal(fs.readFileSync(state, "utf8"), "invalid JSON");
});
