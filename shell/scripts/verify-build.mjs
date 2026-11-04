#!/usr/bin/env node
// 构建门禁：仓库里的 lib/ 与 client/ 必须就是「源码重算出来的那一份」。
// 思路：把入库的产物快照到临时目录 -> 按 package.json 里的 build 步骤原地重算 -> 逐字节比 ->
//       无论结果如何都把快照放回去（漂移不能被这道门禁自己修掉，用户得看见它）。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ARTIFACTS = ['lib', 'client'];
const PROJECTS = ['tsconfig.build.json', 'tsconfig.client.json'];
const IGNORED = new Set(['.DS_Store']);
const VERBOSE = process.argv.includes('--verbose') || process.env.VERIFY_BUILD_VERBOSE === '1';

const rel = (base, full) => path.relative(base, full).split(path.sep).join('/');

function walk(base, dir, out) {
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(base, full, out);
    else if (entry.isFile() && !IGNORED.has(entry.name)) out.set(rel(base, full), fs.readFileSync(full));
  }
  return out;
}

// 以 root 为基准的相对路径 -> 内容，这样快照与重算结果才可比
const readTree = (base) => ARTIFACTS.reduce((m, d) => walk(base, path.join(base, d), m), new Map());
const readArtifacts = () => readTree(root);
const removeArtifacts = () => {
  for (const d of ARTIFACTS) fs.rmSync(path.join(root, d), { recursive: true, force: true });
};

function build() {
  const tscJs = path.join(root, 'node_modules', 'typescript', 'bin', 'tsc');
  if (!fs.existsSync(tscJs)) throw new Error('找不到 node_modules/typescript —— 先在 shell/ 里 pnpm install');
  for (const project of PROJECTS) {
    const r = spawnSync(process.execPath, [tscJs, '-p', project], { cwd: root, stdio: 'inherit' });
    if (r.status !== 0) throw new Error(`tsc -p ${project} 失败（exit ${r.status ?? r.signal}）`);
  }
}

// 谁比谁：expected = 仓库里的那份（可能已经漂移），actual = 刚重算出来的
function compare(expected, actual) {
  const bad = [];
  for (const [file, bytes] of expected) {
    const got = actual.get(file);
    if (!got) bad.push(`${file} —— 仓库里有，重算不出来（源码里已没有这个文件？）`);
    else if (!got.equals(bytes)) bad.push(`${file} —— 内容不一致`);
  }
  for (const file of actual.keys()) if (!expected.has(file)) bad.push(`${file} —— 重算出来了，仓库里没有`);
  return bad;
}

// 第二层：产物入库之后，git 能独立看出「改了产物没提交」。未入库时它是瞎的，必须报「跳过」而不是「通过」。
function gitLayer() {
  const run = (args) => spawnSync('git', args, { cwd: root, encoding: 'utf8' });
  const listed = run(['ls-files', '-z', '--', ...ARTIFACTS]);
  if (listed.status !== 0) return { state: 'skip', why: 'shell/ 不在 git 工作树里' };
  const files = listed.stdout.split('\0').filter(Boolean);
  if (files.length === 0) {
    return { state: 'skip', why: '产物还没入库（git ls-files lib client 为空），git diff 在这条路上永远为空' };
  }
  const diff = run(['diff', '--quiet', '--', ...ARTIFACTS]);
  if (diff.status === 0) return { state: 'pass', count: files.length };
  if (diff.status !== 1) return { state: 'skip', why: `git diff 退出码 ${diff.status}` };
  const named = run(['status', '--porcelain', '-uall', '--', ...ARTIFACTS]);
  return { state: 'fail', count: files.length, files: named.stdout.split('\n').filter(Boolean) };
}

function snapshotToTmp() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'verify-build-'));
  for (const d of ARTIFACTS) {
    const src = path.join(root, d);
    if (fs.existsSync(src)) fs.cpSync(src, path.join(tmp, d), { recursive: true });
  }
  return tmp;
}

function restore(tmp) {
  removeArtifacts();
  for (const d of ARTIFACTS) {
    const from = path.join(tmp, d);
    if (fs.existsSync(from)) fs.cpSync(from, path.join(root, d), { recursive: true });
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}

function main() {
  const tmp = snapshotToTmp();
  let drift;
  try {
    removeArtifacts();
    build();
    drift = compare(readTree(tmp), readArtifacts());
  } catch (err) {
    restore(tmp);
    console.error(`[verify:build] 重算产物失败：${err.message}`);
    return 2;
  }
  restore(tmp);

  const git = gitLayer();
  if (drift.length === 0 && git.state !== 'fail') {
    if (VERBOSE) {
      const n = readArtifacts().size;
      console.log(`[verify:build] ok：lib/ client/ 共 ${n} 个文件与源码重算结果逐字节一致`);
      console.log(`[verify:build] 第二层 git diff：${git.state === 'pass' ? `通过（${git.count} 个文件已入库，无未提交改动）` : `跳过 —— ${git.why}`}`);
    }
    return 0;
  }

  if (drift.length > 0) {
    console.error('[verify:build] 构建产物与源码不一致 —— 改了源码忘了 build。');
    console.error('');
    for (const line of drift) console.error(`  ${line}`);
    console.error('');
    console.error('  重跑 pnpm build，然后把 lib/ 与 client/ 一并提交。');
    console.error('  （仓库里那份产物已原样放回，本门禁不会替你修）');
  }
  const layer2 = git.state === 'pass'
    ? `通过（${git.count} 个文件已入库，无未提交改动）`
    : git.state === 'fail'
      ? `失败（产物已入库，但工作区有未提交的产物改动）`
      : `跳过 —— ${git.why}`;
  console.error(`[verify:build] 第二层 git diff：${layer2}`);
  if (git.state === 'fail') for (const line of git.files) console.error(`  ${line}`);
  return 1;
}

process.exitCode = main();
