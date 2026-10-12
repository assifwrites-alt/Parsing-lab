<?php
// Dr Harry Lab hub (Hostinger / any PHP 7.4+ host). One file, no database.
// The website uploads PDFs here, a worker (Colab / VPS / any Linux box) pulls them, parses with m2.py and
// posts progress + the result zip back. Claude reads/writes commands through the same API.
// Every call needs the token from config.php (header X-Token or ?token=).

declare(strict_types=1);
@set_time_limit(120);
$cfgFile = __DIR__ . '/config.php';
if (!is_file($cfgFile)) { http_response_code(500); header('Content-Type: application/json'); echo json_encode(['ok'=>false,'error'=>'config.php nahi mila: config.sample.php ko config.php naam se copy karke token badlo']); exit; }
$CFG = require $cfgFile;
$DATA = rtrim($CFG['data_dir'] ?? (__DIR__ . '/data'), '/');
foreach (['', '/up', '/files', '/results'] as $d) { if (!is_dir($DATA . $d)) @mkdir($DATA . $d, 0775, true); }
if (!is_file($DATA . '/.htaccess')) @file_put_contents($DATA . '/.htaccess', "Require all denied\nDeny from all\n");

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: X-Token, Content-Type');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') { http_response_code(204); exit; }

function out($d, int $code = 200): void { http_response_code($code); header('Content-Type: application/json; charset=utf-8'); header('Cache-Control: no-store'); echo json_encode($d, JSON_UNESCAPED_UNICODE); exit; }
function bad(string $m, int $code = 400): void { out(['ok' => false, 'error' => $m], $code); }

$tok = $_SERVER['HTTP_X_TOKEN'] ?? ($_GET['token'] ?? '');
$a = $_GET['a'] ?? 'ping';
if ($a === 'ping') out(['ok' => true, 'time' => time(), 'php' => PHP_VERSION, 'auth' => (strlen($tok) > 0 && hash_equals((string)$CFG['token'], (string)$tok))]);
if (!isset($CFG['token']) || $CFG['token'] === 'CHANGE_ME' || !hash_equals((string)$CFG['token'], (string)$tok)) bad('token galat ya config.php mein badla nahi', 401);

// ---- tiny JSON store with locking ----
function jload(string $f, $def) { $s = @file_get_contents($f); if ($s === false || $s === '') return $def; $j = json_decode($s, true); return $j === null ? $def : $j; }
function jmod(string $f, callable $fn) {
    $h = fopen($f, 'c+'); if (!$h) bad('store nahi khula', 500);
    flock($h, LOCK_EX);
    $raw = stream_get_contents($h); $cur = $raw ? json_decode($raw, true) : null; if (!is_array($cur)) $cur = [];
    $new = $fn($cur);
    ftruncate($h, 0); rewind($h); fwrite($h, json_encode($new, JSON_UNESCAPED_UNICODE)); fflush($h); flock($h, LOCK_UN); fclose($h);
    return $new;
}
$JOBS = $DATA . '/jobs.json'; $WORK = $DATA . '/workers.json'; $CMDS = $DATA . '/cmds.jsonl';
function safe_id(string $s): string { return preg_replace('/[^a-zA-Z0-9_-]/', '', $s); }
function safe_name(string $s): string { $s = basename(str_replace('\\', '/', $s)); $s = preg_replace('/[^\p{L}\p{N} ._()\-]/u', '_', $s); return $s === '' ? 'file.pdf' : $s; }
function body_to(string $path, bool $append = false): int {
    $in = fopen('php://input', 'rb'); $o = fopen($path, $append ? 'ab' : 'wb'); $n = 0;
    while (!feof($in)) { $b = fread($in, 1 << 20); if ($b === false || $b === '') break; fwrite($o, $b); $n += strlen($b); }
    fclose($in); fclose($o); return $n;
}
function assemble(string $dir, int $total, string $dest): bool {
    $o = fopen($dest, 'wb'); if (!$o) return false;
    for ($i = 0; $i < $total; $i++) { $p = "$dir/part_$i"; if (!is_file($p)) { fclose($o); @unlink($dest); return false; } $in = fopen($p, 'rb'); stream_copy_to_stream($in, $o); fclose($in); }
    fclose($o);
    for ($i = 0; $i < $total; $i++) @unlink("$dir/part_$i");
    @rmdir($dir); return true;
}
function chunk_count(string $dir, int $total): int { $c = 0; for ($i = 0; $i < $total; $i++) if (is_file("$dir/part_$i")) $c++; return $c; }

switch ($a) {
case 'state': {
    $jobs = jload($JOBS, []); $w = jload($WORK, []); $now = time();
    $cmds = []; if (is_file($CMDS)) { $L = array_slice(file($CMDS, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES), -40); foreach ($L as $l) { $j = json_decode($l, true); if ($j) $cmds[] = $j; } }
    foreach ($w as $k => $x) $w[$k]['ago'] = $now - (int)($x['seen'] ?? 0);
    usort($jobs, fn($x, $y) => ($y['created'] ?? 0) <=> ($x['created'] ?? 0));
    out(['ok' => true, 'now' => $now, 'jobs' => $jobs, 'workers' => array_values($w), 'cmds' => $cmds]);
}
case 'upload': {   // POST raw chunk: id, name, idx, total, size
    $id = safe_id($_GET['id'] ?? ''); $name = safe_name($_GET['name'] ?? 'file.pdf'); $idx = (int)($_GET['idx'] ?? 0); $total = max(1, (int)($_GET['total'] ?? 1)); $size = (int)($_GET['size'] ?? 0);
    if ($id === '' || $idx < 0 || $idx >= $total) bad('id/idx galat');
    if (!preg_match('/\.(pdf|html?|mhtml?|mht)$/i', $name)) bad('sirf PDF ya HTML');
    $dir = "$DATA/up/$id"; if (!is_dir($dir)) @mkdir($dir, 0775, true);
    $n = body_to("$dir/part_$idx");
    if ($n <= 0) { @unlink("$dir/part_$idx"); bad('khali chunk'); }
    $have = chunk_count($dir, $total); $done = false;
    if ($have === $total) {
        if (!assemble($dir, $total, "$DATA/files/$id.pdf")) bad('jodne mein dikkat', 500);
        $sz = filesize("$DATA/files/$id.pdf");
        jmod($JOBS, function ($j) use ($id, $name, $sz) { $j[$id] = ['id' => $id, 'name' => $name, 'size' => $sz, 'status' => 'queued', 'stage' => 'upload ho gayi, worker ka intezar', 'pct' => 0, 'created' => time(), 'updated' => time(), 'jobs' => []]; return $j; });
        $done = true;
    }
    out(['ok' => true, 'have' => $have, 'total' => $total, 'done' => $done]);
}
case 'next': {     // worker claims the oldest queued job
    $wk = substr(safe_id($_GET['worker'] ?? 'worker'), 0, 40); $got = null;
    jmod($JOBS, function ($j) use (&$got, $wk) {
        $q = array_filter($j, fn($x) => ($x['status'] ?? '') === 'queued'); usort($q, fn($x, $y) => ($x['created'] ?? 0) <=> ($y['created'] ?? 0));
        if ($q) { $id = $q[0]['id']; $j[$id]['status'] = 'running'; $j[$id]['worker'] = $wk; $j[$id]['stage'] = 'worker ne utha liya'; $j[$id]['updated'] = time(); $got = $j[$id]; }
        return $j; });
    out(['ok' => true, 'job' => $got]);
}
case 'getpdf': {   // worker downloads (range: off, len)
    $id = safe_id($_GET['id'] ?? ''); $f = "$DATA/files/$id.pdf"; if (!is_file($f)) bad('file nahi', 404);
    $off = max(0, (int)($_GET['off'] ?? 0)); $len = (int)($_GET['len'] ?? 0); $sz = filesize($f); if ($len <= 0 || $off + $len > $sz) $len = $sz - $off;
    header('Content-Type: application/octet-stream'); header('Content-Length: ' . $len); header('X-Total-Size: ' . $sz); header('Cache-Control: no-store');
    $h = fopen($f, 'rb'); fseek($h, $off); $left = $len; while ($left > 0 && !feof($h)) { $b = fread($h, min(1 << 20, $left)); if ($b === false || $b === '') break; echo $b; $left -= strlen($b); } fclose($h); exit;
}
case 'progress': { // worker POST json: id, stage, pct, jobs[]
    $j = json_decode(file_get_contents('php://input'), true); if (!is_array($j) || empty($j['id'])) bad('json galat'); $id = safe_id((string)$j['id']);
    jmod($JOBS, function ($all) use ($id, $j) { if (!isset($all[$id])) return $all;
        $all[$id]['stage'] = substr((string)($j['stage'] ?? ''), 0, 200); $all[$id]['pct'] = max(0, min(100, (float)($j['pct'] ?? 0))); $all[$id]['jobs'] = array_slice($j['jobs'] ?? [], 0, 12); $all[$id]['detail'] = substr((string)($j['detail'] ?? ''), 0, 300); $all[$id]['updated'] = time(); return $all; });
    out(['ok' => true]);
}
case 'result': {   // worker POST raw chunk of the result zip: id, idx, total
    $id = safe_id($_GET['id'] ?? ''); $idx = (int)($_GET['idx'] ?? 0); $total = max(1, (int)($_GET['total'] ?? 1)); if ($id === '' || $idx < 0 || $idx >= $total) bad('galat');
    $dir = "$DATA/up/r_$id"; if (!is_dir($dir)) @mkdir($dir, 0775, true); $n = body_to("$dir/part_$idx"); if ($n <= 0) bad('khali');
    $done = false;
    if (chunk_count($dir, $total) === $total) {
        if (!assemble($dir, $total, "$DATA/results/$id.zip")) bad('jodne mein dikkat', 500);
        $sz = filesize("$DATA/results/$id.zip");
        jmod($JOBS, function ($all) use ($id, $sz) { if (isset($all[$id])) { $all[$id]['status'] = 'done'; $all[$id]['stage'] = 'parsing poori, result tayyar'; $all[$id]['pct'] = 100; $all[$id]['result'] = $sz; $all[$id]['updated'] = time(); } return $all; });
        $done = true;
    }
    out(['ok' => true, 'done' => $done]);
}
case 'fail': {
    $j = json_decode(file_get_contents('php://input'), true); $id = safe_id((string)($j['id'] ?? '')); $err = substr((string)($j['error'] ?? 'error'), 0, 1500);
    jmod($JOBS, function ($all) use ($id, $err) { if (isset($all[$id])) { $all[$id]['status'] = 'failed'; $all[$id]['stage'] = 'fail: ' . $err; $all[$id]['updated'] = time(); } return $all; });
    out(['ok' => true]);
}
case 'requeue': {
    $id = safe_id($_GET['id'] ?? '');
    jmod($JOBS, function ($all) use ($id) { if (isset($all[$id]) && is_file("{$GLOBALS['DATA']}/files/$id.pdf")) { $all[$id]['status'] = 'queued'; $all[$id]['stage'] = 'dobara line mein'; $all[$id]['pct'] = 0; $all[$id]['updated'] = time(); } return $all; });
    out(['ok' => true]);
}
case 'download': { // browser download of the finished result zip
    $id = safe_id($_GET['id'] ?? ''); $f = "$DATA/results/$id.zip"; if (!is_file($f)) bad('result nahi', 404);
    $jobs = jload($JOBS, []); $nm = preg_replace('/\.(pdf|html?|mhtml?|mht)$/i', '', $jobs[$id]['name'] ?? $id);
    header('Content-Type: application/zip'); header('Content-Length: ' . filesize($f)); header('Content-Disposition: attachment; filename="' . str_replace('"', '', $nm) . '_result.zip"'); readfile($f); exit;
}
case 'delete': {
    $id = safe_id($_GET['id'] ?? ''); @unlink("$DATA/files/$id.pdf"); @unlink("$DATA/results/$id.zip");
    jmod($JOBS, function ($all) use ($id) { unset($all[$id]); return $all; }); out(['ok' => true]);
}
case 'hb': {       // worker heartbeat
    $j = json_decode(file_get_contents('php://input'), true) ?: []; $wk = substr(safe_id((string)($j['name'] ?? ($_GET['worker'] ?? 'worker'))), 0, 40);
    jmod($WORK, function ($w) use ($wk, $j) { $w[$wk] = ['name' => $wk, 'host' => substr((string)($j['host'] ?? ''), 0, 60), 'info' => substr((string)($j['info'] ?? ''), 0, 120), 'seen' => time()]; return $w; });
    out(['ok' => true]);
}
case 'cmd': {      // website or Claude posts a command
    $j = json_decode(file_get_contents('php://input'), true); $t = trim((string)($j['text'] ?? '')); if ($t === '') bad('khali command');
    $rec = ['id' => (string)(int)(microtime(true) * 1000), 'ts' => (int)(microtime(true) * 1000), 'text' => substr($t, 0, 1000), 'by' => substr((string)($j['by'] ?? 'web'), 0, 20)];
    file_put_contents($CMDS, json_encode($rec, JSON_UNESCAPED_UNICODE) . "\n", FILE_APPEND | LOCK_EX); out(['ok' => true, 'cmd' => $rec]);
}
case 'cmds': {     // Claude: ?since=<ms>
    $since = (int)($_GET['since'] ?? 0); $r = [];
    if (is_file($CMDS)) foreach (file($CMDS, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $l) { $j = json_decode($l, true); if ($j && ($j['ts'] ?? 0) > $since) $r[] = $j; }
    out(['ok' => true, 'cmds' => $r]);
}
case 'clear': {    // purana data saaf: finished (done) jobs + unke files/results, commands log, purane workers. running/queued/failed bachte hain.
    $removed = 0; $ids = [];
    jmod($JOBS, function ($all) use (&$removed, &$ids) { foreach ($all as $k => $x) { if (($x['status'] ?? '') === 'done') { $ids[] = $k; unset($all[$k]); $removed++; } } return $all; });
    foreach ($ids as $id) { $id = safe_id((string)$id); @unlink("$DATA/files/$id.pdf"); @unlink("$DATA/results/$id.zip"); }
    @file_put_contents($CMDS, '');
    $now = time(); jmod($WORK, function ($w) use ($now) { foreach ($w as $k => $x) { if ($now - (int)($x['seen'] ?? 0) > 600) unset($w[$k]); } return $w; });
    out(['ok' => true, 'removed_jobs' => $removed]);
}
default: bad('a=? samajh nahi aaya', 404);
}
