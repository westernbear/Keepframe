# Fake After Effects

`createAE({state, documents, defaultInterpolation})` returns `{context, serialize, counters, calls, trace}`; JSX executes in a Node `vm` context with `JSON` and the listed ES5+ built-ins deleted.
Unknown host members and unknown property/effect match names throw `fake AE: unsupported <Type>.<name>`; host internals are private.
Arrays, plain objects, and errors returned to JSX belong to its VM realm; host objects and methods cross through strict proxies. Font records, source rectangles, TextDocuments, and KeyframeEases are strict host objects.
This models the Task 7 sync surface; the runner checks the listed ES3 syntax gaps, and broader engine differences remain outside its scope.

## Running

```sh
node --test 'tests/ae_fake/*.test.js' 'extension/test/*.test.js'
node tests/ae_fake/run.js state.json script.jsx entry arg1 arg2 --documents /tmp/documents
.venv/bin/python -m pytest -p no:cacheprovider -q tests/test_ae_fake_runner.py
```

`run.js` creates an empty project when the state file is missing, invokes the named entry with string arguments, atomically replaces state on success, and emits one JSON line `{result, undo_groups, writes, calls}`. `calls` is a method-count map (absent methods have count zero), passed through unchanged by `run_jsx`.
An exception emits `{error, line}` and exits 1; JSX stack locations supply the line, or 0 if unavailable; unsuccessful runs leave persisted state untouched.
`run.js` discards all mutations from a run that throws; real AE keeps partial mutations made before an error.
`checkES3Syntax(source)` runs before JSX execution and rejects `let`/`const` declarations, arrow functions, template literals, classes, trailing commas in object/array literals, and getter/setter literal syntax; errors name the line and use the same `{error, line}` payload.
The context deletes `Array.prototype.indexOf/lastIndexOf/forEach/map/filter/reduce/reduceRight/some/every`, `Array.isArray`, `Object.keys/create/defineProperty/defineProperties/getPrototypeOf/freeze/assign/entries/values`, `String.prototype.trim/trimStart/trimEnd/startsWith/endsWith/includes/padStart/padEnd/repeat`, `Function.prototype.bind`, `Date.now`, `Number.isFinite/isNaN`, and the globals `JSON/Promise/Map/Set/Symbol/Proxy/Reflect`; the fake uses host built-ins internally.
`run_jsx(state_path, script_path, entry, *args, documents=None)` returns the CLI payload, adds `value` when `result` parses as JSON, and skips via `pytest.skip` if Node is absent.
`index.js` loads `fake.test.js` because Node 25 treats the explicit test directory as a module path.

## Supported surface (one line per API)

- `app.version`: read-only string, default `24.6.0x45`, initialized from `state.app.version`.
- `app.project`: the Project host object.
- `app.project.rootFolder`: read-only root FolderItem, also the parent of top-level items.
- Item/folder/layer accesses return fresh wrappers; item/folder `.id` is read-only and persistent, and layer `.index` is live. Mutations resolve wrappers to the same underlying object.
- `app.beginUndoGroup(name)` / `app.endUndoGroup()`: begin increments `counters.undoGroups`; neither increments project writes.
- `app.fonts.allFonts`: native arrays of arrays of strict Font records from `state.app.fonts`.
- `Font.familyName` / `styleName` / `postScriptName`: read-only strings, default empty when unspecified.
- `app.project.items`: ItemCollection with `.length`, 1-based `[i]`, `addComp(name,w,h,pixelAspect,duration,frameRate)`, and `addFolder(name)`.
- `app.project.importFile(ImportOptions)`: creates FootageItem; `.glb` is flagged as a model in serialized state.
- `app.project.file`: read-only File from `state.project.file`, or `null` for an unsaved project.
- `ImportOptions(file)`: callable with or without `new`; holds a settable `.file` that must be a File.
- `FolderItem.name` / `comment` / `parentFolder`: settable; parentFolder must be a FolderItem and cannot create a cycle.
- `FolderItem.items`: live ItemCollection of immediate children, `.length`, 1-based `[i]`, `addComp(...)` and `addFolder(name)`; additions are parented to this folder.
- `FootageItem.name` / `comment` / `parentFolder`: settable item metadata.
- `FootageItem.width` / `height`: read-only dimensions from state; imports default to 1920×1080 without inspecting media.
- `FootageItem.mainSource.file`: File or `null` for generated solid/null sources.
- `FootageItem.file`: read-only File or `null` for generated solid/null sources; follows `replace`.
- `SolidSource.color`: read-only RGB array for generated solid/null footage.
- `FootageItem.replace(file)`: replaces the source File and updates the internal model flag.
- `CompItem.name` / `comment` / `parentFolder`: settable item metadata.
- `CompItem.width` / `height` / `pixelAspect` / `frameRate` / `duration` / `bgColor`: settable settings; bgColor is RGB in 0–1; frameRate, duration and colours store `Math.fround` values.
- `CompItem.renderer` / `renderers`: renderer defaults to Classic 3D's `ADBE Advanced 3d`; the read-only available list defaults to `["ADBE Advanced 3d", "ADBE Ernst", "ADBE Calder"]`; assignments must be listed. Comp state can seed a shorter available list.
- `CompItem.saveFrameToPng(time,file)`: writes a valid RGB PNG at the comp's width/height, filled with the bottom-most enabled non-null solid's colour (black without a solid). Ignores text, media, transforms and compositing; this is sufficient for background-only verification, not a general AE renderer. Counts `calls.saveFrameToPng`, with no project writes or undo group.
- `createAE({frameWriteDelayMs:n,frameWriteFails:true})`: delays PNG output by n ms on a Node timer, or never writes when failure is enabled; the same options may be seeded via `state.testHooks` for CLI host tests. Delayed writes run only after the script returns to Node's event loop; File exists/length reads and blocking `$.sleep` do not flush them. Zero delay writes immediately.
- `CompItem.layers`: LayerCollection with `.length` and 1-based `[i]`; index 1 is the top of the stack.
- `comp.layers.add(item)`: adds AVLayer sourced by a FootageItem or CompItem, at the top.
- `comp.layers.addText(text)`: adds TextLayer with null source, at the top.
- `comp.layers.addSolid(color,name,w,h,pixelAspect,duration)`: creates solid footage and an AVLayer, at the top.
- `comp.layers.addNull(duration)`: creates white 100×100 footage and an AVLayer named Null, at the top.
- `CompItem` / `FootageItem` / `FolderItem` / `AVLayer` / `TextLayer`: constructors exposed for `instanceof`; direct construction throws; TextLayer also satisfies `instanceof AVLayer`.
- `layer.name` / `comment` / `label` / `inPoint` / `outPoint` / `startTime` / `threeDLayer` / `enabled`: settable; label range is 0–16; `enabled` is a boolean, defaults to true and survives serialization.
- `layer.index`: read-only live stack index, updated after every move/removal.
- `layer.source`: read-only source item or null for text.
- `layer.nullLayer`: read-only boolean; true for layers created by `addNull`, preserved in state.
- `layer.parent`: settable layer/null, requires the same comp and rejects cycles; deletion detaches children, with references serialized as layer indices.
- `layer.trackMatteLayer` / `setTrackMatte(layer,TrackMatteType.ALPHA)`: AE 24's read-only matte reference and minimal assignment API; requires a different layer in the same comp, deletion detaches it, serialized as a layer index.
- `AVLayer.replaceSource(item,false)`: replaces footage/comp source, preserving transforms, effects, masks and stack position; text and expression-fixing mode are unmodeled and throw.
- `layer.remove()`: removes the layer; removing an already removed layer throws.
- `layer.moveBefore(layer)` / `moveAfter(layer)` / `moveToBeginning()` / `moveToEnd()`: move within the same comp and update all indices.
- `layer.sourceRectAtTime(t,includeExtents)`: text has width `advance*fontSize*text.length` (advance 0.5 for Arial fonts, 0.6 otherwise), height `fontSize`, left 0, top `-0.8*fontSize`; models are 200×200 with left 0 and top -200; other layers use source dimensions.
- `layer.property(nameOrMatchName)`: returns a supported group by display name or match name; 1-based numeric lookup is also supported; an unknown match name throws where real AE may return null (also applies to PropertyGroup.property).
- `layer.Effects` / `layer.Masks`: aliases for `ADBE Effect Parade` / `ADBE Mask Parade` groups.
- `PropertyGroup.property(nameOrMatchName)` / `numProperties`: child lookup and count, including 1-based numeric property lookup.
- `PropertyGroup.name` / `matchName`: settable display name and read-only match name.
- Effects have a settable boolean `enabled` (default true), and an `ADBE Effect Built In Params` Compositing Options group containing `ADBE Effect Mask Opacity`; groups have no key/value APIs.
- `PropertyType`: `PROPERTY`, `INDEXED_GROUP`, `NAMED_GROUP`; leaf properties and groups expose read-only `propertyType`, and `PropertyValueType.NO_VALUE` is available for leaf filtering.
- `Effects.addProperty("ADBE Linear Wipe")`: effect with `ADBE Linear Wipe-0001` Transition Completion (0), `-0002` Wipe Angle (90), `-0003` Feather (0).
- `Effects.addProperty("ADBE Geometry2")`: effect with `ADBE Geometry2-0001` Anchor Point ([0,0]), `-0002` Position ([0,0]), `-0003` Scale Height (100), `-0004` Scale Width (100), `-0005` Skew (0), `-0006` Skew Axis (0), `-0007` Rotation (0), `-0008` Opacity (100), `-0011` Uniform Scale (1).
- `Masks.addProperty("ADBE Mask Atom")`: creates an empty mask group with a settable display name; mask attributes are unsupported.
- `effect.remove()` / `mask.remove()`: remove the group from its parent; a repeated removal throws.
- `ADBE Transform Group`: exposes `ADBE Anchor Point`, `ADBE Position`, `ADBE Scale`, `ADBE Rotate Z`, and `ADBE Opacity`.
- `ADBE Anchor Point` / `ADBE Position`: `ThreeD_SPATIAL` on every AV layer, including 2D layers; defaults are source center (or [0,0,0] for text) and comp center, with z=0. `ADBE Scale`: `ThreeD`, default [100,100,100]; rotation: 0; opacity: 100. Toggling `threeDLayer` preserves all three components of values, keys and eases.
- `ADBE Position.dimensionsSeparated`: settable boolean; true exposes scalar `ADBE Position_0` / `ADBE Position_1` / `ADBE Position_2`; Z is hidden on 2D layers. Toggling transfers values/keys and joined reads combine all three followers.
- `ADBE Rotate X` / `ADBE Rotate Y` / `ADBE Orientation`: default 0 / 0 / [0,0,0]; hidden on 2D layers; GLB layers start in 3D.
- Hidden `ADBE Position_2` / `ADBE Rotate X` / `ADBE Rotate Y` / `ADBE Orientation` remain readable by name/match name, including values and key metadata, but are excluded from numeric enumeration. Their `setValue`, `setValueAtTime`, `setValuesAtTimes`, `removeKey`, `setTemporalEaseAtKey`, and `setInterpolationTypeAtKey` throw `After Effects error: Can't "set value" on this property because the property or a parent property is hidden.` before mutation. Setting `layer.threeDLayer = true` unhides them, including previously obtained property references; restoration preserves this behavior.
- `ADBE Text Properties` → `ADBE Text Document`: Source Text property containing a detached TextDocument copy.
- `Property.value`: read-only copy sampled at time 0; evaluating an enabled expression throws.
- `Property.setValue(v)`: assigns a static value; throws `fake AE: setValue on a keyframed property` if keys exist. AV anchor/position/scale accept two components and pad z=0/0/100, or three components to set z explicitly; reads always return three (also for setValueAtTime).
- `Property.setValueAtTime(t,v)`: adds a sorted key or replaces the value at an existing time, preserving that key's ease/interpolation metadata.
- `Property.setValuesAtTimes(times,values)`: same time/value validation and replacement semantics as `setValueAtTime`, sorted keys, one write for the batch; non-arrays or unequal lengths throw `fake AE: setValuesAtTimes needs equal arrays`. Both key-writing methods create keys with LINEAR interpolation and temporal ease `{influence: 16.666667, speed: 0}` per ease dimension. `createAE({defaultInterpolation: "BEZIER"})` changes the interpolation for new keys only. A separated Position leader rejects both key-writing methods.
- `Property.numKeys` / `keyTime(i)` / `keyValue(i)` / `removeKey(i)` / `nearestKeyIndex(t)`: 1-based key operations; invalid indices and nearest on an unkeyed property throw.
- `Property.valueAtTime(t,preExpression)`: linearly interpolates scalars/vectors, honors outgoing HOLD, holds beyond endpoints, and holds TextDocuments between keys; BEZIER metadata is preserved but sampled linearly.
- `Property.setInterpolationTypeAtKey(i,inType,outType)` / `keyInInterpolationType(i)` / `keyOutInterpolationType(i)`: store/read interpolation; omitted outType defaults to inType.
- `Property.setTemporalEaseAtKey(i,inEases,outEases)` / `keyInTemporalEase(i)` / `keyOutTemporalEase(i)`: store/read detached ease copies; unseparated AV anchor/position require exactly one ease per side, and AV scale requires exactly three even on 2D layers. Other spatial properties take one, non-spatial vectors one per dimension, and scalars one; omitted outEases defaults to inEases; setting ease switches LINEAR sides to BEZIER, so interpolation must be assigned afterward.
- `Property.expression` / `expressionEnabled`: settable and serialized; assigning nonempty expression enables it, empty disables it; evaluation is unsupported, while `valueAtTime(t,true)` reads underlying animation.
- `Property.canSetExpression` / `propertyValueType` / `matchName` / `name`: read-only property metadata.
- `PropertyValueType`: symbolic `NO_VALUE`, `OneD`, `TwoD`, `TwoD_SPATIAL`, `ThreeD`, `ThreeD_SPATIAL`, `COLOR`, `TEXT_DOCUMENT` constants.
- `KeyframeEase(speed,influence)`: detached value with settable finite speed/influence; influence must be 0.1–100 inclusive.
- `KeyframeInterpolationType`: symbolic `LINEAR`, `BEZIER`, `HOLD` constants.
- `TextDocument(text)`: detached value with settable text, font (default ArialMT), fontSize (36), fillColor ([1,1,1]), applyFill (true), justification (LEFT_JUSTIFY); fillColor reads/writes throw while applyFill is false, including after state restoration.
- `TextDocument.tracking` / `applyStroke` / `strokeColor`: detached unmanaged styling preserved through property assignment; fill colour stores float32 channels.
- `ParagraphJustification`: symbolic `LEFT_JUSTIFY`, `CENTER_JUSTIFY`, `RIGHT_JUSTIFY` constants.
- `$.getenv(name)` / `$.global` / `$.line`: environment from `state.app.env` (missing → null), the VM global object, and fixed line 0.
- `$.sleep(ms)`: blocks with `Atomics.wait` on a SharedArrayBuffer, preventing pending Node frame-write timers from running until JSX returns.
- `File(path)` / `Folder(path)`: callable with or without `new`; read-only absolute `fsName`, `name`, dynamic `exists`, and path string conversion; File `name` is URI encoded and `displayName` is decoded.
- `File.encoding`: settable label, default BINARY; the port reads/writes UTF-8 text regardless of this label.
- `File.open("r"|"w")` / `read()` / `write(text)` / `close()`: buffered text access; failed read open returns false; close flushes write mode.
- `File.length`: dynamic size in bytes, zero when the file does not exist.
- `File.remove()` / `rename(name)`: filesystem operations returning success booleans; rename refuses to overwrite and updates name/fsName.
- `Folder.create()` / `getFiles()`: recursive directory creation and unfiltered File/Folder listing.
- `Folder.myDocuments` / `Folder.userData`: documents argument and LOCALAPPDATA (falling back to documents).
- `Folder.temp`: `<documents>/temp`; `extension_runner.js` supplies the same directory through its injected `os.tmpdir()` so the panel validates and removes only this run's frame files.
- `counters.undoGroups` / `counters.writes`: begin calls and project mutations; same-value assignments count, failed mutations/read operations/hydration/serialization/detached value edits/file I/O do not.
- `createAE().calls`: test-only method-count map for 11 property methods: `setValue`, `setValueAtTime`, `setValuesAtTimes`, `setTemporalEaseAtKey`, `setInterpolationTypeAtKey`, `keyTime`, `keyValue`, `keyInInterpolationType`, `keyOutInterpolationType`, `keyInTemporalEase`, and `keyOutTemporalEase`; also counts `CompItem.saveFrameToPng`. Counts attempted calls, including reads used by fingerprints. Not exposed to JSX or persisted in project state.
- `createAE().trace`: test-only array for neighbour moves, source replacements and ease/interpolation call order (the former `calls` array). Optional `state.testHooks.readProperty` / `writeProperty` inject persistent errors for a match name on property value reads/writes (writes throw before mutation); also not AE APIs or JSX members.

## State

`serialize()` returns plain `{app:{version,fonts,env}, project:{file,rootFolder,items}}`; counters are reset on each `createAE`.
`project.items` is project order; item `parentFolder` and layer `source` are 1-based item references, or null for root/no source.
Items contain persistent id/type/name/comment and type-specific settings; CompItem layers are stored top-to-bottom; each layer contains all property groups, effects and masks.
Property records contain value, sorted keys with time/value/inEases/outEases/inInterpolation/outInterpolation, expression metadata, matchName/name/propertyValueType, and position separation state.
TextDocuments and KeyframeEases serialize to plain field objects; fonts/env can also be supplied as top-level seed fields.
RenderQueue, ShapeLayer, scheduling, project saving, other effects/mask attributes, media decoding, and expression evaluation fail when requested or remain outside this modeled surface.

`kfRender` schedules exports and returns frame paths immediately. The real panel core in `extension_runner.js` polls asynchronously every 250 ms until each file has a positive stable size, PNG signature and terminal IEND chunk, then streams uploads. This permits delayed fake writes to exercise the complete render job; the per-frame wait is 60 seconds and the combined wait is 10 minutes. The CLI `run.js` also lets scheduled timers finish after its entry point returns; its JSON result does not imply that a delayed frame already exists.
