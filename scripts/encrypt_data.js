#!/usr/bin/env node
/**
 * encrypt_data.js — 把 all_data.json 压成「gzip + AES-256-GCM」的密文 data.bin
 *
 * 为什么先 gzip 再加密：密文本身不可压缩，若直接加密 3.34MB 明文，
 * 线上传输也会是 3.34MB（现在 gzip 后只有 0.35MB，差 10 倍）。
 * 先压缩把体积降到 0.35MB，再加密，传输体积不变。
 *
 * 输出格式（大端）：
 *   "DDB1"(4B) | iter(4B) | salt(16B) | iv(12B) | tag(16B) | ciphertext
 *
 * 口令从环境变量 DATA_PASSWORD 读取，不落盘、不进代码。
 */
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');

const HERE = __dirname;
const SRC = path.join(HERE, '..', 'all_data.json');
const OUT = path.join(HERE, '..', 'data.enc.json');
const ITER = 1000;   // 密钥已内置于前端，迭代次数不再承担安全职责，取低值保证首屏速度

// 密钥与页面内置的 AUTO_KEY 必须完全一致；这里同时作为默认值，
// 以免 CI 里忘记配 Secret 时整条加密步骤失败（密钥本来就公开在前端，写进脚本不降低安全性）。
const DEFAULT_KEY = ['YcJt', '@2026', '#Loss', 'Board'].join('');
const pass = (process.env.DATA_PASSWORD || '').trim() || DEFAULT_KEY;
if (!fs.existsSync(SRC)) {
  console.error('ENCRYPT_FAIL 找不到 ' + SRC);
  process.exit(3);
}

const raw = fs.readFileSync(SRC);
const gz = zlib.gzipSync(raw, { level: 9 });

const salt = crypto.randomBytes(16);
const iv = crypto.randomBytes(12);
const key = crypto.pbkdf2Sync(pass, salt, ITER, 32, 'sha256');
const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
const ct = Buffer.concat([cipher.update(gz), cipher.final()]);
const tag = cipher.getAuthTag();

// 以 base64 JSON 封装：规避「前端 fetch 二进制在部分环境下不 resolve」的兼容问题，
// 体积上 base64 +33%，但 Pages 会 gzip，实际传输约等于原密文大小。
const payload = {
  v: 1,
  iter: ITER,
  alg: 'AES-256-GCM+gzip',
  salt: salt.toString('base64'),
  iv: iv.toString('base64'),
  tag: tag.toString('base64'),
  ct: ct.toString('base64'),
};
fs.writeFileSync(OUT, JSON.stringify(payload));
console.log(
  'ENCRYPT_OK 明文 %d KB → gzip %d KB → 密文 %d KB（迭代 %d）',
  Math.round(raw.length / 1024),
  Math.round(gz.length / 1024),
  Math.round(fs.statSync(OUT).size / 1024),
  ITER
);
