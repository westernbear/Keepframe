"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const zlib = require("node:zlib");
const { spawnSync } = require("node:child_process");
const { createAE } = require("./ae");
const { checkES3Syntax } = require("./run");

test("frame export writes a valid full-size PNG using the bottom enabled solid", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-png-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const ae = createAE({ documents: dir }), c = ae.context;
  assert.equal(path.dirname(c.Folder.temp.fsName), dir);
  const folder = new c.Folder(c.Folder.temp.fsName + "/frames");
  assert.equal(folder.create(), true);
  const comp = c.app.project.items.addComp("PNG", 3, 2, 1, 1, 30);
  const bottom = comp.layers.addSolid([1, 0.5, 0], "bottom", 3, 2, 1, 1);
  comp.layers.addSolid([0, 1, 0], "top", 3, 2, 1, 1);
  comp.layers.addText("ignored");
  const file = new c.File(folder.fsName + "/frame.png");
  const writes = ae.counters.writes;
  function pixels() {
    const png = fs.readFileSync(file.fsName);
    assert.deepEqual(png.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    assert.equal(png.readUInt32BE(16), 3); assert.equal(png.readUInt32BE(20), 2);
    assert.equal(png[24], 8); assert.equal(png[25], 2);
    assert.equal(png.readUInt32BE(png.length - 4), 0xae426082);
    const chunks = [];
    for (let offset = 8; offset < png.length;) {
      const size = png.readUInt32BE(offset), kind = png.toString("ascii", offset + 4, offset + 8);
      if (kind === "IDAT") chunks.push(png.subarray(offset + 8, offset + 8 + size));
      offset += size + 12;
    }
    return [...zlib.inflateSync(Buffer.concat(chunks))];
  }
  comp.saveFrameToPng(0, file);
  assert.equal(file.exists, true); assert.ok(file.length > 0);
  assert.deepEqual(pixels(), [0, 255,128,0, 255,128,0, 255,128,0, 0, 255,128,0, 255,128,0, 255,128,0]);
  assert.equal(ae.counters.writes, writes); assert.equal(ae.counters.undoGroups, 0);
  assert.equal(ae.calls.saveFrameToPng, 1);
  bottom.enabled = false;
  comp.saveFrameToPng(0, file);
  assert.deepEqual(pixels(), [0, 0,255,0, 0,255,0, 0,255,0, 0, 0,255,0, 0,255,0, 0,255,0]);
  comp.layers[2].enabled = false;
  comp.saveFrameToPng(0, file);
  assert.deepEqual(pixels(), Array(20).fill(0));
  assert.equal(file.remove(), true); assert.equal(file.exists, false); assert.equal(file.length, 0);
  assert.throws(() => comp.saveFrameToPng(-1, file), /time/);
  assert.throws(() => comp.saveFrameToPng(0, "bad"), /File/);
});

test("delayed and missing frame writes run on a timer after JSX returns", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-png-delay-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  for (const fails of [false, true]) {
    const ae = createAE({ documents: dir, frameWriteDelayMs: 30, frameWriteFails: fails }), c = ae.context;
    const comp = c.app.project.items.addComp("PNG", 2, 2, 1, 1, 30);
    const file = new c.File(dir + "/delayed-" + fails + ".png");
    comp.saveFrameToPng(0, file);
    assert.equal(file.exists, false);
    const started = Date.now();
    c.$.sleep(50);
    assert.ok(Date.now() - started >= 45);
    assert.equal(file.exists, false); assert.equal(file.length, 0);
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(file.exists, !fails); assert.equal(file.length > 0, !fails);
  }
});

function fixture(options) {
  const ae = createAE(options);
  const comp = ae.context.app.project.items.addComp("Test", 640, 480, 1, 5, 30);
  const layer = comp.layers.addText("Hello");
  const transform = layer.property("ADBE Transform Group");
  return { ...ae, comp, layer, transform, opacity: transform.property("ADBE Opacity") };
}

function syncFixture(desired, options) {
  const f = createAE(options);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../extension/host/keepframe.jsx"), "utf8"), f.context);
  const spec = {schema: "keepframe.ae-comp/1", project: "demo", comp: {tag: "kf", name: "C",
    width: 640, height: 480, fps: 30, frames: 60}, assets: [], warnings: [],
    layers: desired.map((id, i) => ({id, order: desired.length - i, kind: "null", name: id, label: null,
      in: 0, out: 59, source: null, anchor: [0, 0], warnings: [], effects: {reveal: null, skew: null},
      props: {position_x: [[0, 0, null, null]], position_y: [[0, 0, null, null]],
        scale: [[0, [100, 100], null, null]], rotation: [[0, 0, null, null]], opacity: [[0, 100, null, null]]}}))};
  const sync = () => JSON.parse(f.context.kfSync(JSON.stringify(spec), "{}", "false"));
  assert.equal(sync().ok, true);
  return {...f, spec, sync, comp: f.context.app.project.items[3]};
}

function stackLayers(comp) {
  return Array.from({length: comp.layers.length}, (_, i) => comp.layers[i + 1]);
}

test("fix8: font metrics and layer enabled survive persistence", () => {
  const f = fixture();
  const text = f.layer.property("ADBE Text Properties").property("ADBE Text Document");
  const doc = text.value;
  doc.font = "Example"; doc.fontSize = 20; text.setValue(doc);
  const wide = f.layer.sourceRectAtTime(0, false).width;
  doc.font = "ArialMT"; text.setValue(doc);
  assert.ok(f.layer.sourceRectAtTime(0, false).width < wide);
  doc.fontSize = 10; text.setValue(doc);
  assert.equal(f.layer.sourceRectAtTime(0, false).height, 10);
  f.layer.enabled = false;
  const restored = createAE({state: f.serialize()}).context.app.project.items[1].layers[1];
  assert.equal(restored.enabled, false);
  restored.enabled = true;
  assert.equal(restored.enabled, true);
  assert.throws(() => { restored.enabled = 1; }, /enabled/);
});

for (const probe of ["user-moved bottom pair", "changed order with trailing pair"]) {
  test(`fix2: order probe ${probe} converges in one sync`, () => {
    const desired = probe === "user-moved bottom pair" ? ["d", "c", "b", "a", "bg"] : ["r0", "r1", "r2", "t0", "t1"];
    const f = syncFixture(probe === "user-moved bottom pair" ? desired : ["t1", "t0", "r0", "r1", "r2"]);
    if (probe === "user-moved bottom pair") {
      f.comp.layers[5].moveToBeginning(); f.comp.layers[5].moveToBeginning();
      assert.deepEqual(stackLayers(f.comp).map((l) => l.name), ["a", "bg", "d", "c", "b"]);
    } else {
      for (const s of f.spec.layers) s.order = desired.length - desired.indexOf(s.id);
    }
    f.trace.length = 0;
    assert.equal(f.sync().applied, true);
    assert.deepEqual(stackLayers(f.comp).map((l) => l.name), desired);
    assert.deepEqual(f.trace.filter((c) => /^move/.test(c.operation)).map((c) => c.name), desired.slice(-2));
    const writes = f.counters.writes;
    assert.equal(f.sync().applied, true);
    assert.equal(f.counters.writes - writes, 0);
  });
}

test("fix2: random tagged permutations preserve users and kept anchors, then write zero", () => {
  let seed = 0x8ae2;
  const random = (limit) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % limit; };
  for (let n = 3; n <= 7; n++) for (let users = 0; users <= 3; users++) for (let trial = 0; trial < 20; trial++) {
    const desired = Array.from({length: n}, (_, i) => `t${i}`), f = syncFixture(desired);
    const shuffled = stackLayers(f.comp);
    for (let i = n - 1; i > 0; i--) { const j = random(i + 1); [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]]; }
    for (let i = 0; i < users; i++) {
      const user = f.comp.layers.addText(`USER${i}`); user.name = `USER${i}`;
      shuffled.splice(random(shuffled.length + 1), 0, user);
    }
    for (const l of shuffled.slice().reverse()) l.moveToBeginning();
    const before = shuffled.map((l) => l.name), current = before.filter((name) => desired.includes(name));
    const userState = f.serialize().project.items.find((i) => i.type === "CompItem").layers.filter((l) => !l.comment);
    // Independent exhaustive subsequence oracle (at most 128 subsets for these small stacks).
    let longest = 0;
    for (let mask = 0; mask < 1 << n; mask++) {
      const ranks = current.flatMap((name, i) => mask & (1 << i) ? [desired.indexOf(name)] : []);
      if (ranks.every((r, i) => !i || ranks[i - 1] < r)) longest = Math.max(longest, ranks.length);
    }
    f.trace.length = 0;
    const response = f.sync(), scenario = JSON.stringify({n, users, trial, before});
    assert.equal(response.applied, true, scenario);
    const after = stackLayers(f.comp).map((l) => l.name);
    assert.deepEqual(after.filter((name) => desired.includes(name)), desired, scenario);
    const moves = f.trace.filter((c) => /^move/.test(c.operation)), moved = moves.map((c) => c.name);
    assert.equal(moved.length, n - longest, scenario);
    assert.equal(new Set(moved).size, moved.length, scenario);
    const kept = desired.filter((id) => !moved.includes(id)), placed = new Set(kept);
    for (const move of moves) {
      assert.ok(placed.has(move.target), `neighbour must already be placed: ${scenario}`);
      assert.equal(desired.indexOf(move.target), desired.indexOf(move.name) + (move.operation === "moveBefore" ? 1 : -1), scenario);
      placed.add(move.name);
    }
    const userNames = before.filter((name) => !desired.includes(name));
    assert.deepEqual(after.filter((name) => userNames.includes(name)), userNames, scenario);
    for (const user of userNames) for (const anchor of kept) {
      assert.equal(before.indexOf(user) < before.indexOf(anchor), after.indexOf(user) < after.indexOf(anchor), scenario);
    }
    assert.deepEqual(f.serialize().project.items.find((i) => i.type === "CompItem").layers.filter((l) => !l.comment), userState, scenario);
    const writes = f.counters.writes;
    assert.equal(f.sync().applied, true, scenario);
    assert.equal(f.counters.writes - writes, 0, scenario);
  }
});

test("fix2: TextDocument fillColor throws on read and write while fill is disabled", () => {
  const f = fixture(), p = f.layer.property("Text").property("Source Text"), doc = p.value;
  doc.applyFill = false;
  assert.throws(() => doc.fillColor, /fill.*disabled/);
  assert.throws(() => { doc.fillColor = [1, 0, 0]; }, /fill.*disabled/);
  p.setValue(doc);
  const restored = createAE({state: f.serialize()}).context.app.project.items[1].layers[1].property("Text").property("Source Text").value;
  assert.equal(restored.applyFill, false);
  assert.throws(() => restored.fillColor, /fill.*disabled/);
  assert.throws(() => { restored.fillColor = [1, 0, 0]; }, /fill.*disabled/);
  restored.applyFill = true; restored.fillColor = [1, 0, 0];
  assert.deepEqual(Array.from(restored.fillColor), [1, 0, 0]);
});

test("review: AE stores settings and colors in single precision", () => {
  const f = fixture();
  f.comp.frameRate = 29.97; f.comp.duration = 60 / 29.97;
  assert.equal(f.comp.frameRate, Math.fround(29.97));
  assert.equal(f.comp.duration, Math.fround(60 / 29.97));
  const solid = f.comp.layers.addSolid([17 / 255, 34 / 255, 51 / 255], "S", 10, 10, 1, 1);
  assert.deepEqual(Array.from(solid.source.mainSource.color), [17 / 255, 34 / 255, 51 / 255].map(Math.fround));
  const p = f.layer.property("Text").property("Source Text"), doc = p.value;
  doc.fillColor = [17 / 255, 34 / 255, 51 / 255]; p.setValue(doc);
  assert.equal(p.value.fillColor[0], Math.fround(17 / 255));
});

test("review: renderer availability is read-only and assignments must be listed", () => {
  const f = fixture();
  assert.equal(f.comp.renderer, "ADBE Advanced 3d");
  assert.deepEqual(Array.from(f.comp.renderers), ["ADBE Advanced 3d", "ADBE Ernst", "ADBE Calder"]);
  f.comp.renderer = "ADBE Calder";
  assert.throws(() => { f.comp.renderer = "ADBE Classic 3d"; }, /invalid renderer/);
  assert.throws(() => { f.comp.renderers = []; }, /read-only/);
});

test("review: effects have compositing groups and enabled state", () => {
  const f = fixture(), effect = f.layer.Effects.addProperty("ADBE Linear Wipe");
  const group = effect.property("ADBE Effect Built In Params");
  assert.equal(group.propertyType, f.context.PropertyType.NAMED_GROUP);
  assert.equal(effect.property(1).propertyType, f.context.PropertyType.PROPERTY);
  assert.throws(() => group.numKeys, /unsupported/);
  assert.throws(() => group.valueAtTime, /unsupported/);
  assert.equal(group.property("ADBE Effect Mask Opacity").value, 100);
  assert.equal(effect.enabled, true); effect.enabled = false;
  assert.equal(createAE({state: f.serialize()}).context.app.project.items[1].layers[1].Effects.property(1).enabled, false);
});

test("review: parenting and matte references detach when a layer is removed", () => {
  const f = fixture(), target = f.comp.layers.addNull();
  f.layer.parent = target; f.layer.setTrackMatte(target, f.context.TrackMatteType.ALPHA);
  assert.equal(f.layer.parent.index, target.index);
  assert.equal(f.layer.trackMatteLayer.index, target.index);
  assert.throws(() => { target.parent = f.layer; }, /cyclic/);
  assert.throws(() => { f.layer.trackMatteLayer = target; }, /read-only/);
  const restored = createAE({state: f.serialize()}).context.app.project.items[1];
  assert.equal(restored.layers[2].parent.index, 1);
  assert.equal(restored.layers[2].trackMatteLayer.index, 1);
  target.remove();
  assert.equal(f.layer.parent, null); assert.equal(f.layer.trackMatteLayer, null);
});

test("review: item and layer wrappers are fresh but preserve stable identity", () => {
  const f = fixture(), project = f.context.app.project;
  assert.notEqual(project.items[1], project.items[1]);
  assert.equal(project.items[1].id, f.comp.id);
  assert.notEqual(project.rootFolder, project.rootFolder);
  assert.equal(project.rootFolder.id, f.comp.parentFolder.id);
  assert.notEqual(f.comp.layers[1], f.comp.layers[1]);
  assert.equal(f.comp.layers[1].index, f.layer.index);
  const folder = project.items.addFolder("F"); f.comp.parentFolder = folder;
  assert.equal(folder.items[1].id, f.comp.id);
  const state = f.serialize();
  assert.equal(createAE({state}).context.app.project.items[1].id, f.comp.id);
  assert.throws(() => f.layer.moveBefore(f.comp.layers[1]), /itself/);
});

test("review: ease can switch linear interpolation; host must set interpolation last", () => {
  const f = fixture(); f.opacity.setValueAtTime(0, 10);
  f.opacity.setTemporalEaseAtKey(1, [new f.context.KeyframeEase(0, 33)]);
  assert.equal(f.opacity.keyInInterpolationType(1), "BEZIER");
  const hostSource = fs.readFileSync(path.join(__dirname, "../../extension/host/keepframe.jsx"), "utf8");
  vm.runInContext(hostSource, f.context);
  const spec = {schema: "keepframe.ae-comp/1", project: "demo", comp: {tag: "kf", name: "C", width: 640,
    height: 480, fps: 30, frames: 60}, assets: [], warnings: [], layers: [{id: "x", kind: "null", name: "x",
    label: null, order: 0, in: 0, out: 59, source: null, anchor: [0, 0], warnings: [],
    props: {position_x: [[0, 0, [[25, 100]], null], [30, 100, null, [[25, 100]]]], position_y: [[0, 0, null, null]],
      scale: [[0, [100, 100], null, null]], rotation: [[0, 0, null, null]], opacity: [[0, 100, null, null]]},
    effects: {reveal: null, skew: null}}]};
  assert.equal(JSON.parse(f.context.kfSync(JSON.stringify(spec), "{}", "false")).ok, true);
  const temporal = f.trace.filter((call) => ["ease", "interpolation"].includes(call.operation));
  assert.deepEqual(temporal.slice(-4).map((call) => call.operation), ["ease", "interpolation", "ease", "interpolation"]);
});

test("review: File names are URI encoded and display names are decoded", () => {
  const f = createAE(), file = new f.context.File("/tmp/한글 project.aep");
  assert.equal(file.name, encodeURI("한글 project.aep"));
  assert.equal(file.displayName, "한글 project.aep");
});

test("review: TextDocument exposes detached unmanaged styling", () => {
  const f = fixture(), p = f.layer.property("Text").property("Source Text"), doc = p.value;
  doc.tracking = 42; doc.applyStroke = true; doc.strokeColor = [1, 0, 0]; p.setValue(doc);
  const again = p.value;
  assert.equal(again.tracking, 42); assert.equal(again.applyStroke, true);
  again.tracking = 0; assert.equal(p.value.tracking, 42);
});

test("review: fault hooks throw before mutation and on property reads", () => {
  const f = fixture({state: {testHooks: {writeProperty: "ADBE Opacity", readProperty: "ADBE Scale"}}});
  const before = f.counters.writes;
  assert.throws(() => f.opacity.setValue(5), /injected write/);
  assert.equal(f.counters.writes, before);
  assert.throws(() => f.transform.property("ADBE Scale").valueAtTime(0, true), /injected read/);
});

test("review: hydration of an empty saved project never reuses the root item id", () => {
  const seed = createAE().serialize(), f = createAE({state: seed});
  const folder = f.context.app.project.items.addFolder("Keepframe");
  assert.notEqual(folder.id, f.context.app.project.rootFolder.id);
  assert.deepEqual(createAE({state: f.serialize()}).serialize(), f.serialize());
});

test("review: host insertion and reordering move only new layers or layers outside the existing LIS", () => {
  const hostSource = fs.readFileSync(path.join(__dirname, "../../extension/host/keepframe.jsx"), "utf8");
  const layerSpec = (id, order) => ({id, order, kind: "null", name: id, label: null, in: 0, out: 59,
    source: null, anchor: [0, 0], warnings: [], effects: {reveal: null, skew: null},
    props: {position_x: [[0, 0, null, null]], position_y: [[0, 0, null, null]],
      scale: [[0, [100, 100], null, null]], rotation: [[0, 0, null, null]], opacity: [[0, 100, null, null]]}});
  for (const change of ["ordered", "insert", "decrease", "swap", "combined"]) {
    const f = createAE(); vm.runInContext(hostSource, f.context);
    const spec = {schema: "keepframe.ae-comp/1", project: "demo", comp: {tag: "kf", name: "C",
      width: 640, height: 480, fps: 30, frames: 60}, assets: [], warnings: [],
      layers: [layerSpec("background", 0), layerSpec("a", 1), layerSpec("b", 2), layerSpec("d", 3)]};
    const sync = () => JSON.parse(f.context.kfSync(JSON.stringify(spec), "{}", "false"));
    assert.equal(sync().ok, true);
    const comp = f.context.app.project.items[3], user = comp.layers.addText("USER-TOP");
    user.name = "USER-TOP"; f.trace.length = 0;
    if (change === "insert") spec.layers.push(layerSpec("c", 1.5));
    if (change === "decrease") spec.layers[3].order = -1;
    if (change === "swap") { spec.layers[1].order = 2; spec.layers[2].order = 1; }
    if (change === "combined") {
      // Existing [d,b,a,bg] => [a,d,b,bg], LIS d/b/bg must all stay.
      spec.layers[1].order = 4; spec.layers.push(layerSpec("c", 3.5));
    }
    assert.equal(sync().ok, true);
    const moves = f.trace.filter((call) => /^move/.test(call.operation));
    const expected = {ordered: [], insert: ["c"], decrease: ["d"], swap: ["a"], combined: ["a", "c"]}[change];
    assert.deepEqual(moves.map((call) => call.comment.split(";")[0].replace(/^keepframe:/, "")), expected, change);
    for (const move of moves) assert.notEqual(move.name, move.target);
    f.trace.length = 0; assert.equal(sync().ok, true);
    assert.deepEqual(f.trace, [], change);
  }
});

test("sync kind detection and renderer are strict, serialized AE attributes", () => {
  const f = fixture();
  assert.equal(f.comp.renderer, "ADBE Advanced 3d");
  assert.equal(f.comp.parentFolder.id, f.context.app.project.rootFolder.id);
  assert.throws(() => { f.context.app.project.rootFolder = null; }, /read-only/);
  f.comp.renderer = "ADBE Calder";
  assert.throws(() => { f.comp.renderer = "bogus"; }, /invalid renderer/);
  const nul = f.comp.layers.addNull();
  const solid = f.comp.layers.addSolid([1, 0, 0], "Solid", 10, 10, 1, 1);
  assert.equal(nul.nullLayer, true);
  assert.equal(solid.nullLayer, false);
  assert.equal(f.layer.nullLayer, false);
  assert.throws(() => { nul.nullLayer = false; }, /read-only/);
  assert.equal(nul.source.file, null);
  const footage = f.context.app.project.importFile(new f.context.ImportOptions(new f.context.File("x.png")));
  assert.equal(footage.file.name, "x.png");
  footage.replace(new f.context.File("new.png"));
  assert.equal(footage.file.name, "new.png");
  assert.throws(() => { footage.file = null; }, /read-only/);
  const state = f.serialize();
  const restored = createAE({ state });
  assert.equal(restored.context.app.project.items[1].renderer, "ADBE Calder");
  assert.equal(restored.context.app.project.items[1].layers[2].nullLayer, true);
});

test("replaceSource updates footage and rectangles without touching layer properties or extras", () => {
  const f = fixture();
  const solid = f.comp.layers.addSolid([1, 0, 0], "Solid", 20, 30, 1, 2);
  const other = f.comp.layers.addSolid([0, 1, 0], "Other", 40, 60, 1, 2);
  const mask = solid.Masks.addProperty("ADBE Mask Atom");
  mask.name = "User mask";
  solid.property("ADBE Transform Group").property("ADBE Opacity").setValue(55);
  solid.replaceSource(other.source, false);
  assert.equal(solid.source.id, other.source.id);
  assert.equal(solid.sourceRectAtTime(0, false).width, 40);
  assert.equal(solid.Masks.property(1).name, "User mask");
  assert.equal(solid.property("ADBE Transform Group").property("ADBE Opacity").value, 55);
  assert.throws(() => solid.replaceSource(null, false), /requires footage or comp/);
  assert.throws(() => solid.replaceSource(other.source, true), /unsupported/);
  assert.throws(() => f.layer.replaceSource(other.source, false), /unsupported/);
  const state = f.serialize();
  assert.equal(state.project.items[0].layers[1].source, state.project.items[0].layers[0].source);
});

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
  assert.deepEqual(Array.from(position.valueAtTime(1, true)), [10, 20, 0]);
  const copy = position.keyValue(1);
  copy[0] = 999;
  assert.deepEqual(Array.from(position.keyValue(1)), [0, 10, 0]);
});

test("setValuesAtTimes writes sorted linear keys with default ease in one call", () => {
  const f = fixture(), p = f.opacity, writes = f.counters.writes;
  p.setValuesAtTimes([2, 0, 1], [80, 0, 40]);
  assert.equal(p.numKeys, 3);
  assert.equal(p.keyTime(1), 0);
  assert.equal(p.keyValue(2), 40);
  assert.equal(p.keyInInterpolationType(2), "LINEAR");
  assert.equal(p.keyOutInterpolationType(2), "LINEAR");
  assert.equal(p.keyInTemporalEase(2)[0].influence, 16.666667);
  assert.equal(p.keyOutTemporalEase(2)[0].speed, 0);
  assert.equal(f.counters.writes, writes + 1);
  assert.equal(f.calls.setValuesAtTimes, 1);
  assert.equal(f.calls.setValueAtTime || 0, 0);
  p.setInterpolationTypeAtKey(2, "HOLD");
  p.setTemporalEaseAtKey(2, [new f.context.KeyframeEase(4, 25)]);
  p.setValuesAtTimes([1, 3], [45, 100]);
  assert.equal(p.keyValue(2), 45);
  assert.equal(p.keyInInterpolationType(2), "HOLD");
  assert.equal(p.keyInTemporalEase(2)[0].influence, 25);
  assert.equal(p.numKeys, 4);
  assert.equal(f.calls.setInterpolationTypeAtKey, 1);
  assert.equal(f.calls.setTemporalEaseAtKey, 1);
});

test("setValuesAtTimes validates arrays, finite times, values and dimensions", () => {
  const f = fixture(), p = f.opacity, writes = f.counters.writes;
  for (const [times, values] of [[[0, 1], [10]], [null, []], [[], {}]]) {
    assert.throws(() => p.setValuesAtTimes(times, values),
      {message: "fake AE: setValuesAtTimes needs equal arrays"});
  }
  for (const [times, values] of [[[NaN], [10]], [[Infinity], [10]], [["0"], [10]],
    [[0], [NaN]], [[0], ["10"]], [[0], [[10]]]]) {
    assert.throws(() => p.setValuesAtTimes(times, values), /invalid/);
  }
  const scale = f.transform.property("ADBE Scale");
  assert.throws(() => scale.setValuesAtTimes([0], [[100]]), /dimensions/);
  assert.equal(f.counters.writes, writes);
  assert.equal(p.numKeys, 0);
  scale.setValuesAtTimes([0, 1], [[100, 200], [50, 60, 70]]);
  assert.deepEqual(Array.from(scale.keyValue(1)), [100, 200, 100]);
  assert.deepEqual(Array.from(scale.keyInTemporalEase(1), e => [e.speed, e.influence]),
    [[0, 16.666667], [0, 16.666667], [0, 16.666667]]);
  const text = f.layer.property("Text").property("Source Text"), doc = text.value;
  doc.text = "Batch";
  text.setValuesAtTimes([0, 1], [doc, doc]);
  doc.text = "Detached";
  assert.equal(text.keyValue(2).text, "Batch");
});

test("setValuesAtTimes respects hidden properties, separated position and write faults", () => {
  const f = fixture(), position = f.transform.property("ADBE Position");
  position.dimensionsSeparated = true;
  const z = f.transform.property("ADBE Position_2"), writes = f.counters.writes;
  assert.throws(() => z.setValuesAtTimes([0], [10]), /parent property is hidden/);
  assert.throws(() => position.setValuesAtTimes([0], [[10, 20]]), /separated position/);
  assert.equal(f.counters.writes, writes);
  f.layer.threeDLayer = true;
  z.setValuesAtTimes([0, 1], [10, 20]);
  assert.equal(z.numKeys, 2);
  const faulty = fixture({state: {testHooks: {writeProperty: "ADBE Opacity"}}});
  const before = faulty.counters.writes;
  assert.throws(() => faulty.opacity.setValuesAtTimes([0], [10]), /injected write/);
  assert.equal(faulty.counters.writes, before);
});

test("defaultInterpolation can model BEZIER for script-created keys", () => {
  const f = fixture({defaultInterpolation: "BEZIER"});
  f.opacity.setValuesAtTimes([0, 1, 2], [0, 50, 100]);
  assert.equal(f.opacity.keyInInterpolationType(1), "BEZIER");
  assert.equal(f.opacity.keyOutInterpolationType(3), "BEZIER");
  f.opacity.setValueAtTime(3, 80);
  assert.equal(f.opacity.keyInInterpolationType(4), "BEZIER");
  assert.equal(f.calls.setValueAtTime, 1);
});

for (const eased of [false, true]) {
  test(`host guards BEZIER defaults before applying explicit ease (eased=${eased})`, () => {
    const f = syncFixture(["x"], {defaultInterpolation: "BEZIER"});
    f.spec.layers[0].props.position_x = eased ?
      [[0, 0, [[25, 100]], null], [12, 100, null, [[75, 0]]], [24, 50, null, null]] :
      [[0, 0, null, null], [12, 100, null, null], [24, 50, null, null]];
    const reads = f.calls.keyInInterpolationType || 0;
    assert.equal(f.sync().applied, true);
    assert.equal(f.calls.keyInInterpolationType - reads, 4, "one guard read plus three fingerprint reads");
    assert.equal(f.calls.setValuesAtTimes, 1);
    assert.equal(f.calls.setValueAtTime || 0, 0);
    assert.equal(f.calls.setTemporalEaseAtKey || 0, eased ? 2 : 0);
    assert.equal(f.calls.setInterpolationTypeAtKey, 3);
    const x = f.comp.layers[1].property("Transform").property("ADBE Position_0");
    assert.deepEqual(Array.from({length: 3}, (_, i) => [x.keyInInterpolationType(i + 1),
      x.keyOutInterpolationType(i + 1)]), eased ?
      [["LINEAR", "BEZIER"], ["BEZIER", "LINEAR"], ["LINEAR", "LINEAR"]] :
      [["LINEAR", "LINEAR"], ["LINEAR", "LINEAR"], ["LINEAR", "LINEAR"]]);
    const writes = f.counters.writes;
    assert.equal(f.sync().applied, true);
    assert.equal(f.counters.writes, writes);
  });
}

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
  assert.throws(() => p.setTemporalEaseAtKey(1, [ease, ease], [ease, ease]), /dimensions/);
  p.setTemporalEaseAtKey(1, [ease, ease, ease], [ease, ease, ease]);
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
  assert.equal(p.propertyValueType, context.PropertyValueType.ThreeD);
  assert.throws(() => p.setValueAtTime(1, [100]), /dimensions/);
});

test("separated position exposes readable scalar X/Y/Z on 2D layers and survives toggling", () => {
  const { transform, layer } = fixture();
  const p = transform.property("ADBE Position");
  p.setValue([10, 20]);
  p.dimensionsSeparated = true;
  const x = transform.property("ADBE Position_0");
  const y = transform.property("ADBE Position_1");
  const z = transform.property("ADBE Position_2");
  assert.equal(x.value, 10);
  assert.equal(y.value, 20);
  assert.equal(z.value, 0);
  x.setValueAtTime(0, 10);
  x.setValueAtTime(2, 30);
  y.setValue(40);
  layer.threeDLayer = true;
  z.setValue(50);
  layer.threeDLayer = false;
  assert.equal(x.valueAtTime(1, false), 20);
  assert.deepEqual(Array.from(p.valueAtTime(1, false)), [20, 40, 50]);
  p.dimensionsSeparated = false;
  assert.deepEqual(Array.from(p.valueAtTime(1, false)), [20, 40, 50]);
  assert.throws(() => transform.property("ADBE Position_0"), /unsupported/);
  p.dimensionsSeparated = true;
  assert.equal(transform.property("ADBE Position_0").valueAtTime(1, false), 20);
  assert.throws(() => p.setValue([0, 0]), /separated position/);
});

for (const match of ["ADBE Position_2", "ADBE Rotate X", "ADBE Rotate Y", "ADBE Orientation"]) {
  test(`hidden 2D property ${match} permits reads and rejects all five mutations until 3D`, () => {
    const f = fixture(), c = f.context;
    f.transform.property("ADBE Position").dimensionsSeparated = true;
    const p = f.transform.property(match), value = match === "ADBE Orientation" ? [7, 8, 9] : 7;
    const eases = Array(match === "ADBE Orientation" ? 3 : 1).fill(new c.KeyframeEase(2, 40));
    const error = {message: 'After Effects error: Can\'t "set value" on this property because the property or a parent property is hidden.'};
    assert.deepEqual(JSON.parse(JSON.stringify(p.value)), match === "ADBE Orientation" ? [0, 0, 0] : 0);
    assert.throws(() => p.setValue(value), error);
    f.layer.threeDLayer = true;
    p.setValue(value); p.setValueAtTime(0, value);
    p.setTemporalEaseAtKey(1, eases);
    p.setInterpolationTypeAtKey(1, c.KeyframeInterpolationType.HOLD);
    f.layer.threeDLayer = false;
    const before = f.serialize(), writes = f.counters.writes;
    assert.deepEqual(JSON.parse(JSON.stringify(p.valueAtTime(0, true))), value);
    assert.deepEqual(JSON.parse(JSON.stringify(p.keyValue(1))), value);
    assert.equal(p.numKeys, 1); assert.equal(p.keyTime(1), 0); assert.equal(p.nearestKeyIndex(0), 1);
    assert.equal(p.keyInTemporalEase(1)[0].speed, 2);
    assert.equal(p.keyOutTemporalEase(1)[0].influence, 40);
    assert.equal(p.keyInInterpolationType(1), "HOLD"); assert.equal(p.keyOutInterpolationType(1), "HOLD");
    assert.ok(!Array.from({length: f.transform.numProperties}, (_, i) => f.transform.property(i + 1).matchName).includes(match));
    for (const operation of [() => p.setValue(value), () => p.setValueAtTime(1, value), () => p.removeKey(1),
      () => p.setTemporalEaseAtKey(1, eases), () => p.setInterpolationTypeAtKey(1, c.KeyframeInterpolationType.LINEAR)]) {
      assert.throws(operation, error);
    }
    assert.equal(f.counters.writes, writes);
    assert.deepEqual(f.serialize(), before);
    const restored = createAE({state: before}), layer = restored.context.app.project.items[1].layers[1];
    const hidden = layer.property("ADBE Transform Group").property(match);
    assert.deepEqual(JSON.parse(JSON.stringify(hidden.keyValue(1))), value);
    assert.throws(() => hidden.removeKey(1), error);
    layer.threeDLayer = true;
    hidden.removeKey(1); hidden.setValue(value); hidden.setValueAtTime(1, value);
    hidden.setTemporalEaseAtKey(1, Array(eases.length).fill(new restored.context.KeyframeEase(0, 33)));
    hidden.setInterpolationTypeAtKey(1, restored.context.KeyframeInterpolationType.LINEAR);
    assert.equal(hidden.numKeys, 1);
  });
}

test("layers add on top; all moves and removal update indices", () => {
  const { comp, layer: a } = fixture();
  const b = comp.layers.addNull(5);
  const c = comp.layers.addSolid([1, 0, 0], "Solid", 20, 30, 1, 5);
  const order = (expected) => {
    assert.equal(comp.layers.length, expected.length);
    expected.forEach((layer, i) => {
      assert.equal(comp.layers[i + 1].index, layer.index);
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
  assert.equal(folder.items[1].id, image.id);
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
  assert.equal(modelLayer.sourceRectAtTime(0, false).left, 0);
  assert.equal(modelLayer.sourceRectAtTime(0, false).top, -200);
  assert.deepEqual(Array.from(modelLayer.property("ADBE Transform Group").property("ADBE Orientation").value), [0, 0, 0]);
  const text = f.layer.property("ADBE Text Properties").property("ADBE Text Document");
  const doc = text.value;
  doc.fontSize = 10;
  doc.fillColor = [1, 0.5, 0];
  doc.justification = c.ParagraphJustification.CENTER_JUSTIFY;
  text.setValue(doc);
  doc.text = "not committed";
  assert.equal(text.value.text, "Hello");
  const tr = f.layer.sourceRectAtTime(0, false);
  assert.equal(tr.width, 25);
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
  assert.equal(wipe.numProperties, 4);
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
  assert.equal(comp.layers[1].source.id, restored.context.app.project.items[6].id);
  assert.equal(comp.parentFolder.items[1].id, comp.id);
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
  assert.equal(child.parentFolder.id, folder.id);
  before = counters.writes;
  const comp = child.items.addComp("Comp", 10, 20, 1, 5, 30);
  assert.equal(counters.writes, before + 1);
  assert.equal(comp.parentFolder.id, child.id);
  assert.equal(folder.items[1].id, child.id);
  assert.equal(child.items[1].id, comp.id);
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
  assert.deepEqual(JSON.parse(first.stdout), { result: `literal $\`" arg:${path.basename(dir)}`, undo_groups: 0, writes: 1, calls: {} });
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

test("JSX receives context-realm values, protected methods, and Error instances", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-realms-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, "file.txt"), "x");
  const ae = createAE({ documents: dir, state: { app: { fonts: [[{ familyName: "Test" }]], env: { DATA: { x: [1] } } } } });
  assert.equal(vm.runInContext(`
    function check(ok, message) { if (!ok) throw Error(message); }
    var comp = app.project.items.addComp("C", 640, 480, 1, 5, 30);
    var layer = comp.layers.addText("Hi");
    var p = layer.property("ADBE Transform Group").property("ADBE Position");
    check(p.value instanceof Array, "value realm");
    check(p.value.constructor.constructor("return typeof JSON")() === "undefined", "host JSON");
    p.setValueAtTime(0, [1, 2]); p.setValueAtTime(2, [3, 4]);
    check(p.keyValue(1) instanceof Array, "keyValue realm");
    check(p.valueAtTime(1, true) instanceof Array, "sample realm");
    p.setTemporalEaseAtKey(1, [new KeyframeEase(0, 33)], [new KeyframeEase(0, 33)]);
    check(p.keyInTemporalEase(1) instanceof Array, "ease array realm");
    check(File.prototype.constructor.constructor("return typeof JSON")() === "undefined", "constructor prototype realm");
    var rect = layer.sourceRectAtTime(0, false);
    check(rect instanceof Object, "rect realm");
    check(app.project.items instanceof Object && comp.layers instanceof Object, "collection realm");
    check(app.fonts.allFonts instanceof Array && app.fonts.allFonts[0] instanceof Array, "font array realm");
    check(Folder.myDocuments.getFiles() instanceof Array, "file array realm");
    check(comp.bgColor instanceof Array, "setting realm");
    check(layer.property("Text").property("Source Text").value.fillColor instanceof Array, "document color realm");
    check($.getenv("DATA") instanceof Object && $.getenv("DATA").x instanceof Array, "plain object realm");
    var solid = comp.layers.addSolid([1, 0, 0], "S", 10, 10, 1, 5);
    check(solid.source.mainSource.color instanceof Array, "solid color realm");
    p.dimensionsSeparated = true;
    check(p.value instanceof Array, "separated realm");
    var calls = [
      function() { app.bogus; }, function() { layer.label = 17; },
      function() { comp.name = {}; comp.layers.add(null); },
      function() { new KeyframeEase(0, 0); }, function() { p.keyValue(0); },
      function() { new ImportOptions(null); }, function() { CompItem(); },
      function() { comp.layers.length = 3; }, function() { app.bogus = 1; },
      function() { delete app.project; }, function() { app.__proto__; },
      function() { Folder("missing-folder").getFiles(); },
      function() { File.prototype.bogus(); },
      function() { app.beginUndoGroup.constructor("return JSON")(); },
      function() { Object.getOwnPropertyDescriptor(p, "value").get.constructor("return JSON")(); }
    ];
    for (var i = 0; i < calls.length; i++) {
      var caught = false;
      try { calls[i](); } catch (e) {
        caught = true; check(e instanceof Error, "error realm " + i);
        check(e.constructor.constructor("return typeof JSON")() === "undefined", "error JSON");
      }
      check(caught, "expected error " + i);
    }
    check(layer instanceof TextLayer && layer instanceof AVLayer && comp instanceof CompItem, "AE instanceof");
    "ok";
  `, ae.context, { filename: "realm.jsx" }), "ok");
});

test("temporal ease dimensions follow spatial, scale, and scalar value types", () => {
  const { context: c, layer, transform } = fixture();
  const ease = new c.KeyframeEase(0, 33);
  for (const threeD of [false, true]) {
    layer.threeDLayer = threeD;
    for (const match of ["ADBE Position", "ADBE Anchor Point", "ADBE Scale"]) {
      const p = transform.property(match);
      const spatial = match !== "ADBE Scale", size = spatial ? 1 : 3;
      assert.equal(p.propertyValueType, c.PropertyValueType[spatial ? "ThreeD_SPATIAL" : "ThreeD"]);
      p.setValueAtTime(0, p.value);
      assert.equal(p.keyInTemporalEase(1).length, size);
      assert.equal(p.keyOutTemporalEase(1).length, size);
      const eases = Array(size).fill(ease);
      p.setTemporalEaseAtKey(1, eases, eases);
      assert.equal(p.keyInTemporalEase(1).length, size);
      assert.equal(p.keyOutTemporalEase(1).length, size);
      assert.throws(() => p.setTemporalEaseAtKey(1, Array(size + 1).fill(ease)), /dimensions/);
      assert.throws(() => p.setTemporalEaseAtKey(1, Array(size - 1).fill(ease)), /dimensions/);
    }
    const position = transform.property("ADBE Position");
    position.dimensionsSeparated = true;
    for (let i = 0; i < (threeD ? 3 : 2); i++) {
      const p = transform.property(`ADBE Position_${i}`);
      assert.equal(p.propertyValueType, c.PropertyValueType.OneD);
      p.setValueAtTime(0, p.value);
      p.setTemporalEaseAtKey(1, [ease], [ease]);
      assert.equal(p.keyInTemporalEase(1).length, 1);
      assert.throws(() => p.setTemporalEaseAtKey(1, [ease, ease]), /dimensions/);
    }
    position.dimensionsSeparated = false;
    assert.equal(position.keyInTemporalEase(1).length, 1);
  }
});

test("all AV transforms pad two-component writes and preserve z when toggling 3D", () => {
  const { context: c, comp, layer, serialize } = fixture();
  const matches = ["ADBE Anchor Point", "ADBE Position", "ADBE Scale"];
  const image = c.app.project.importFile(new c.ImportOptions(new c.File("asset.png")));
  const model = c.app.project.importFile(new c.ImportOptions(new c.File("asset.glb")));
  const nested = c.app.project.items.addComp("Nested", 320, 180, 1, 5, 30);
  const layers = [layer, comp.layers.addNull(5), comp.layers.addSolid([1, 0, 0], "S", 20, 30, 1, 5),
    comp.layers.add(image), comp.layers.add(nested), comp.layers.add(model)];
  assert.equal(layers[5].threeDLayer, true);
  for (const av of layers) {
    const transform = av.property("ADBE Transform Group");
    matches.forEach((match) => {
      const p = transform.property(match), scale = match === "ADBE Scale", z = scale ? 100 : 0;
      assert.equal(p.propertyValueType, c.PropertyValueType[scale ? "ThreeD" : "ThreeD_SPATIAL"]);
      assert.equal(p.value.length, 3);
      assert.equal(p.value[2], z);
      p.setValue([1, 2, 7]);
      assert.deepEqual(Array.from(p.value), [1, 2, 7]);
      p.setValue([10, 20]);
      assert.deepEqual(Array.from(p.value), [10, 20, z]);
      p.setValueAtTime(0, [10, 20]); p.setValueAtTime(2, [30, 40, 8]);
      assert.deepEqual(Array.from(p.keyValue(1)), [10, 20, z]);
      assert.deepEqual(Array.from(p.keyValue(2)), [30, 40, 8]);
      assert.deepEqual(Array.from(p.valueAtTime(1, true)), [20, 30, (z + 8) / 2]);
      p.setValueAtTime(2, [30, 40]);
      assert.deepEqual(Array.from(p.keyValue(2)), [30, 40, z]);
      for (const bad of [[1], [1, 2, 3, 4], [1, 2, NaN]]) {
        assert.throws(() => p.setValueAtTime(3, bad), /invalid/);
      }
      assert.equal(p.keyInTemporalEase(1).length, scale ? 3 : 1);
    });
    const position = transform.property("ADBE Position");
    position.dimensionsSeparated = true;
    const z = transform.property("ADBE Position_2");
    av.threeDLayer = true;
    z.setValueAtTime(0, 5); z.setValueAtTime(2, 15);
    for (const threeD of [false, true, false]) {
      av.threeDLayer = threeD;
      assert.deepEqual(Array.from(position.valueAtTime(1, true)), [20, 30, 10]);
      assert.equal(transform.property("ADBE Position_2").keyValue(2), 15);
      for (const match of ["ADBE Rotate X", "ADBE Rotate Y", "ADBE Orientation"]) {
        assert.ok(transform.property(match));
      }
      matches.slice(0, 1).concat(matches.slice(2)).forEach((match) => {
        const p = transform.property(match), scale = match === "ADBE Scale";
        assert.deepEqual(Array.from(p.keyValue(2)), [30, 40, scale ? 100 : 0]);
        assert.equal(p.keyInTemporalEase(1).length, scale ? 3 : 1);
      });
    }
    position.dimensionsSeparated = false;
    assert.deepEqual(Array.from(position.keyValue(2)), [30, 40, 15]);
  }
  const state = serialize();
  assert.deepEqual(createAE({ state }).serialize(), state);
});

test("ES5+ built-ins are deleted only from the JSX context", () => {
  const { context } = createAE();
  const paths = [
    ...["indexOf", "lastIndexOf", "forEach", "map", "filter", "reduce", "reduceRight", "some", "every"].map((n) => `Array.prototype.${n}`),
    "Array.isArray",
    ...["keys", "create", "defineProperty", "defineProperties", "getPrototypeOf", "freeze", "assign", "entries", "values"].map((n) => `Object.${n}`),
    ...["trim", "trimStart", "trimEnd", "startsWith", "endsWith", "includes", "padStart", "padEnd", "repeat"].map((n) => `String.prototype.${n}`),
    "Function.prototype.bind", "Date.now", "Number.isFinite", "Number.isNaN",
    "JSON", "Promise", "Map", "Set", "Symbol", "Proxy", "Reflect",
  ];
  paths.forEach((name) => assert.equal(vm.runInContext(`typeof ${name}`, context), "undefined", name));
  assert.equal(typeof Array.prototype.map, "function");
  assert.equal(typeof Object.keys, "function");
  assert.equal(typeof JSON, "object");
});

test("CLI rejects each requested ES3 syntax violation with its line before execution", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-syntax-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = path.join(dir, "syntax.jsx"), state = path.join(dir, "state.json");
  const examples = [
    ["let x = 1;", /let declaration/], ["const x = 1;", /const declaration/],
    ["var f = (x) => x;", /arrow function/], ["var x = `hello`;", /template literal/],
    ["class X {}", /class/], ["var x = { a: 1, };", /trailing comma.*object/],
    ["var x = [1, ];", /trailing comma.*array/],
    ["var x = { get a() { return 1; } };", /getter/],
    ["var x = { set a(v) {} };", /setter/],
    ["var x = { 'a': 1, /*comment*/ };", /trailing comma.*object/],
    ["var x = { get 'a'() { return 1; } };", /getter/],
    ["var x = { nested: { a: [1, 2,] } };", /trailing comma.*array/],
    ["var x = { a: true ? 1 : { b: 2, } };", /trailing comma.*object/],
    ["var x = 1 + { a: 1, };", /trailing comma.*object/],
    ["var x = +{ get a() { return 1; } };", /getter/],
    ["var x = 1; x += { a: 1, };", /trailing comma.*object/],
    ["if (true) [1,];", /trailing comma.*array/],
    ["{} [1,];", /trailing comma.*array/],
    ["let 이름 = 1;", /let declaration/],
    ["const \\u0061 = 1;", /const declaration/],
    ["var f = function() {} / 2; let x = 1;", /let declaration/],
    ["var f = function named() {} / 2; const x = 1;", /const declaration/],
  ];
  for (const [source, message] of examples) {
    fs.writeFileSync(script, 'app.project.items.addFolder("must not execute");\n\n' + source + '\nfunction entry() { return "ok"; }\n');
    const result = spawnSync(process.execPath, [path.join(__dirname, "run.js"), state, script, "entry"], { encoding: "utf8" });
    assert.equal(result.status, 1, source);
    const payload = JSON.parse(result.stdout);
    assert.match(payload.error, message, source);
    assert.match(payload.error, /line 3/, source);
    assert.equal(payload.line, 3, source);
    assert.equal(fs.existsSync(state), false, source);
  }
});

test("ES3 syntax checking ignores comments, strings, regexes, and ordinary ES3 constructs", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ae-es3-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const script = path.join(dir, "es3.jsx"), state = path.join(dir, "state.json");
  fs.writeFileSync(script, [
    '// let x = 1; const y = 2; class X {}; => `template`',
    '/* { get a() {} } [1,] {a: 1,} */',
    'function entry() {',
    '  var text = "let const class => ` { get a() {} } [1,]";',
    '  var obj = { get: 1, set: 2, nested: { x: [1, 2] }, fn: function() { return 3; } };',
    '  var rx = /let const class => ` \\/ [a,] /;',
    '  if (true) /class =>/.test(text);',
    '  if (false) {} else {} /class =>/.test(text);',
    '  var n = 8 / 2 / 2;',
    '  for (var i = 0; i < 1; i++) { obj.get += i; }',
    '  return String(obj.fn() + n);',
    '}',
  ].join("\n"));
  const result = spawnSync(process.execPath, [path.join(__dirname, "run.js"), state, script, "entry"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.equal(JSON.parse(result.stdout).result, "5");
});

test("ES3 checker reports multiline locations and preserves ordinary ES3 identifiers", () => {
  const examples = [
    ["// const ignored\r\n/* class ignored\r\n */\r\nlet x = 1;", "let declaration", 4],
    ["var x = {\n a: 1,\n};", "trailing comma in object literal", 2],
    ["var x = [\n 1,\n];", "trailing comma in array literal", 2],
    ["var x = {\n get a() { return 1; }\n};", "getter literal syntax", 2],
    ["var f = function() {} / 2;\nconst x = 1;", "const declaration", 2],
  ];
  for (const [source, rule, line] of examples) {
    assert.throws(() => checkES3Syntax(source), { message: `fake AE: ES3 syntax: ${rule} at line ${line}`, line });
  }
  assert.doesNotThrow(() => checkES3Syntax('var let = 1; let += 2; var 이름 = 1; var \\u0061 = 2;'));
  assert.doesNotThrow(() => checkES3Syntax('var f = function() {} / 2; function g() {} /class =>/.test("class");'));
});

test('final: AE rejects inPoint at or beyond outPoint', () => {
  const f = fixture();
  for (const value of [f.layer.outPoint, f.layer.outPoint + 1])
    assert.throws(() => { f.layer.inPoint = value; }, /inPoint.*outPoint/);
  f.layer.outPoint = 7;
  f.layer.inPoint = 5;
  assert.equal(f.layer.inPoint, 5);
});
