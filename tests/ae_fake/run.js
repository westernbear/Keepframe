"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const { createAE } = require("./ae");

function checkES3Syntax(source) {
  const tokens = [], brackets = [];
  let offset = 0, line = 1, regexAllowed = true;
  const fail = (rule, token) => {
    const error = Error(`fake AE: ES3 syntax: ${rule} at line ${token.line}`);
    error.line = token.line;
    throw error;
  };
  const advance = (end) => {
    line += (source.slice(offset, end).match(/\r\n|[\r\n\u2028\u2029]/g) || []).length;
    offset = end;
  };
  const prefixes = /^(return|throw|case|delete|void|typeof|new|in|instanceof|else|do)$/;
  // ponytail: token checks cover the listed ES3 gaps; use an ES3 parser for full grammar validation.
  while (offset < source.length) {
    const start = offset, c = source[offset], next = source[offset + 1];
    if (/\s/.test(c)) { advance(offset + (c === "\r" && next === "\n" ? 2 : 1)); continue; }
    if (c === "/" && next === "/") {
      const end = source.slice(offset).search(/[\r\n\u2028\u2029]/);
      advance(end < 0 ? source.length : offset + end); continue;
    }
    if (c === "/" && next === "*") {
      const end = source.indexOf("*/", offset + 2);
      advance(end < 0 ? source.length : end + 2); continue;
    }
    const token = { value: c, kind: "punctuation", line };
    let end = offset + 1;
    if (c === "`") fail("template literal", token);
    if (c === "'" || c === '"') {
      while (end < source.length) {
        if (source[end] === "\\") end += 2;
        else if (source[end++] === c) break;
      }
      token.kind = "literal";
    } else if (c === "/" && regexAllowed) {
      let inClass = false;
      while (end < source.length) {
        const ch = source[end++];
        if (ch === "\\") end++;
        else if (ch === "[") inClass = true;
        else if (ch === "]") inClass = false;
        else if (ch === "/" && !inClass) break;
        else if (/[\r\n\u2028\u2029]/.test(ch)) break;
      }
      while (end < source.length && /[a-z]/i.test(source[end])) end++;
      token.kind = "literal";
    } else if (/[\p{ID_Start}$_]/u.test(c) || c === "\\" && next === "u") {
      const word = /^(?:[\p{ID_Start}$_]|\\u[\da-fA-F]{4})(?:[\p{ID_Continue}$\u200c\u200d]|\\u[\da-fA-F]{4})*/u.exec(source.slice(offset));
      if (word) end = offset + word[0].length;
      token.kind = "word";
    } else if (/\d/.test(c)) {
      const number = /^(?:0[xX][\da-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/.exec(source.slice(offset));
      end = offset + number[0].length; token.kind = "literal";
    } else {
      const operator = /^(?:=>|===|!==|>>>|<<=|>>=|==|!=|<=|>=|\+\+|--|&&|\|\||<<|>>|[+*\/%&|^!-]=)/.exec(source.slice(offset));
      if (operator) end = offset + operator[0].length;
    }
    token.value = source.slice(start, end);
    if (token.kind === "word") token.value = token.value.replace(/\\u([\da-fA-F]{4})/g, (_match, digits) => String.fromCharCode(parseInt(digits, 16)));
    if (token.value === "=>") fail("arrow function", token);
    const previous = tokens[tokens.length - 1], parent = brackets[brackets.length - 1];
    const expressionBefore = previous && (previous.kind === "word" && prefixes.test(previous.value) && !["else", "do"].includes(previous.value)
      || previous.kind === "punctuation" && regexAllowed && ![";", "{", "}"].includes(previous.value));
    if (token.value === "function" && previous?.value !== ".") token.functionExpression = Boolean(expressionBefore);
    token.propertyStart = parent?.kind === "object" && (previous?.value === "{" || previous?.value === ",");
    if (["(", "[", "{"].includes(token.value)) {
      let kind = "block";
      if (token.value === "(") kind = "paren";
      if (token.value === "[") kind = regexAllowed ? "array" : "index";
      if (token.value === "{" && expressionBefore) kind = "object";
      if (token.value === "{" && previous?.functionExpression) kind = "functionExpression";
      const fn = previous?.value === "function" ? previous : tokens[tokens.length - 2]?.value === "function" ? tokens[tokens.length - 2] : undefined;
      brackets.push({ kind, control: token.value === "(" && /^(if|while|for|with|switch|catch)$/.test(previous?.value),
        functionExpression: token.value === "(" ? fn?.functionExpression : undefined });
      regexAllowed = true;
    } else if ([")", "]", "}"].includes(token.value)) {
      const closing = brackets.pop();
      if (token.value === ")") token.functionExpression = closing?.functionExpression;
      if (previous?.value === "," && ["object", "array"].includes(closing?.kind)) fail(`trailing comma in ${closing.kind} literal`, previous);
      regexAllowed = Boolean(closing?.control || closing?.kind === "block");
    } else regexAllowed = token.kind === "word" ? prefixes.test(token.value)
      : token.kind !== "literal" && ![".", "++", "--"].includes(token.value);
    tokens.push(token); advance(end);
  }
  tokens.forEach((token, i) => {
    const previous = tokens[i - 1], next = tokens[i + 1];
    if (token.kind !== "word" || previous?.value === ".") return;
    if (["let", "const"].includes(token.value) && (next?.kind === "word" || ["[", "{"].includes(next?.value))) fail(`${token.value} declaration`, token);
    if (token.value === "class" && (next?.kind === "word" || next?.value === "{")) fail("class syntax", token);
    if (token.propertyStart && ["get", "set"].includes(token.value) && ["word", "literal"].includes(next?.kind)
      && tokens[i + 2]?.value === "(") fail(`${token.value === "get" ? "getter" : "setter"} literal syntax`, token);
  });
}

if (require.main === module) {
  let scriptPath;
  try {
    const args = process.argv.slice(2);
    const flag = args.indexOf("--documents");
    let documents;
    if (flag !== -1) {
      if (!args[flag + 1]) throw Error("--documents requires a directory");
      documents = args[flag + 1];
      args.splice(flag, 2);
    }
    const [statePath, script, entry, ...entryArgs] = args;
    scriptPath = script;
    if (!statePath || !script || !entry) throw Error("usage: node run.js <state.json> <script.jsx> <entry> [args...] [--documents <dir>]");
    if (!/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(entry)) throw Error("entry must be a function name");
    let state = {};
    try { state = JSON.parse(fs.readFileSync(statePath, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    const source = fs.readFileSync(script, "utf8");
    checkES3Syntax(source);
    const ae = createAE({ state, documents });
    vm.runInContext(source, ae.context, { filename: script });
    // Invoke in the VM as well, preserving its stack/line information. Args are data.
    const call = `${entry}(${entryArgs.map((arg) => JSON.stringify(arg)).join(",")})`;
    const result = vm.runInContext(call, ae.context, { filename: "fake-ae-entry" });
    if (typeof result !== "string") throw Error("entry must return a string");
    const temporary = `${statePath}.${process.pid}.tmp`;
    try {
      fs.writeFileSync(temporary, JSON.stringify(ae.serialize(), null, 2) + "\n", "utf8");
      fs.renameSync(temporary, statePath);
    } finally {
      fs.rmSync(temporary, { force: true });
    }
    process.stdout.write(JSON.stringify({ result, undo_groups: ae.counters.undoGroups, writes: ae.counters.writes }) + "\n");
  } catch (error) {
    const stackLine = scriptPath && String(error?.stack || "").split("\n").find((line) => line.includes(`${scriptPath}:`));
    const location = stackLine && stackLine.slice(stackLine.indexOf(`${scriptPath}:`) + scriptPath.length + 1).match(/^\d+/);
    const line = Number(error?.line || location?.[0] || 0);
    process.stdout.write(JSON.stringify({ error: String(error?.message ?? error), line }) + "\n");
    process.exitCode = 1;
  }
}

module.exports = { checkES3Syntax };
