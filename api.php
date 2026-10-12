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
if (empty($CFG['anthropic_key']) && is_file(__DIR__ . '/key.php')) { $k = require __DIR__ . '/key.php'; if (is_string($k) && $k !== '') $CFG['anthropic_key'] = trim($k); }
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
    out(['ok' => true, 'now' => $now, 'jobs' => $jobs, 'workers' => array_values($w), 'cmds' => $cmds, 'agent' => !empty($CFG['anthropic_key'])]);
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
    $id = safe_id($_GET['id'] ?? ''); @unlink("$DATA/files/$id.pdf"); @unlink("$DATA/results/$id.zip"); @unlink("$DATA/logs/$id.log");
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
    foreach ($ids as $id) { $id = safe_id((string)$id); @unlink("$DATA/files/$id.pdf"); @unlink("$DATA/results/$id.zip"); @unlink("$DATA/logs/$id.log"); @unlink("$DATA/review/$id.json"); }
    @file_put_contents($CMDS, '');
    $now = time(); jmod($WORK, function ($w) use ($now) { foreach ($w as $k => $x) { if ($now - (int)($x['seen'] ?? 0) > 600) unset($w[$k]); } return $w; });
    out(['ok' => true, 'removed_jobs' => $removed]);
}
case 'files': {    // list files inside a finished result zip
    $id = safe_id($_GET['id'] ?? ''); $f = "$DATA/results/$id.zip"; if (!is_file($f)) bad('result nahi', 404);
    if (!class_exists('ZipArchive')) bad('server par zip support nahi hai', 500);
    $z = new ZipArchive(); if ($z->open($f) !== true) bad('zip khula nahi', 500);
    $r = []; for ($i = 0; $i < $z->numFiles && count($r) < 400; $i++) { $st = $z->statIndex($i); if (substr($st['name'], -1) === '/') continue; $r[] = ['name' => $st['name'], 'size' => (int)$st['size']]; }
    $z->close(); out(['ok' => true, 'files' => $r]);
}
case 'file': {     // stream one file from the result zip (exact name only; nothing is extracted to disk)
    $id = safe_id($_GET['id'] ?? ''); $f = "$DATA/results/$id.zip"; $name = (string)($_GET['name'] ?? ''); if (!is_file($f) || $name === '') bad('result nahi', 404);
    if (!class_exists('ZipArchive')) bad('server par zip support nahi hai', 500);
    $z = new ZipArchive(); if ($z->open($f) !== true) bad('zip khula nahi', 500);
    $i = $z->locateName($name); if ($i === false) bad('file nahi mili', 404);
    $st = $z->statIndex($i); if ($st['size'] > 64 * 1024 * 1024) bad('file bahut badi hai, zip download karo', 413);
    $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
    $types = ['json' => 'application/json; charset=utf-8', 'txt' => 'text/plain; charset=utf-8', 'md' => 'text/plain; charset=utf-8', 'log' => 'text/plain; charset=utf-8', 'html' => 'text/plain; charset=utf-8', 'csv' => 'text/plain; charset=utf-8', 'jpg' => 'image/jpeg', 'jpeg' => 'image/jpeg', 'png' => 'image/png', 'gif' => 'image/gif', 'webp' => 'image/webp'];
    header('Content-Type: ' . ($types[$ext] ?? 'application/octet-stream')); header('X-Content-Type-Options: nosniff'); header('Cache-Control: no-store'); header('Content-Length: ' . $st['size']);
    if (isset($_GET['dl'])) header('Content-Disposition: attachment; filename="' . str_replace(['"', "\r", "\n"], '', basename($name)) . '"');
    $h = $z->getStream($name); if (!$h) bad('file padhi nahi gayi', 500); fpassthru($h); fclose($h); $z->close(); exit;
}
case 'log': {      // worker posts diagnostics (probe + end of run.log); Claude reads data/logs/<id>.log
    $id = safe_id($_GET['id'] ?? ''); if ($id === '') bad('id galat');
    $dir = "$DATA/logs"; if (!is_dir($dir)) @mkdir($dir, 0775, true);
    $raw = (string)file_get_contents('php://input'); if (strlen($raw) > 60000) $raw = substr($raw, -60000);
    file_put_contents("$dir/$id.log", $raw, LOCK_EX); out(['ok' => true]);
}
case 'think': {    // run the on-hub Claude agent for web command ?id=<cmd id> (fire-and-forget from the website)
    if (empty($CFG['anthropic_key'])) bad('anthropic_key config.php mein nahi hai', 501);
    @ignore_user_abort(true); @set_time_limit(280);
    $id = safe_id((string)($_GET['id'] ?? '')); $cmd = null;
    if (is_file($CMDS)) foreach (file($CMDS, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $l) { $j = json_decode($l, true); if ($j && ($j['id'] ?? '') === $id) $cmd = $j; }
    if (!$cmd || ($cmd['by'] ?? '') !== 'web') bad('command nahi mili', 404);
    $lk = fopen("$DATA/think.lock", 'c'); if (!$lk || !flock($lk, LOCK_EX | LOCK_NB)) bad('Claude abhi ek aur kaam par hai', 429);
    $st = jmod("$DATA/think.json", function ($x) use ($id) { $x['done'] = $x['done'] ?? []; $x['runs'] = array_values(array_filter($x['runs'] ?? [], fn($t) => time() - $t < 3600)); $x['dup'] = in_array($id, $x['done'], true); $x['limit'] = count($x['runs']) >= 15; if (!$x['dup'] && !$x['limit']) { $x['runs'][] = time(); $x['done'][] = $id; $x['done'] = array_slice($x['done'], -100); } return $x; });
    if ($st['dup']) bad('ye command pehle chal chuki', 409);
    $post = function (string $t) use ($CMDS) { file_put_contents($CMDS, json_encode(['id' => (string)(int)(microtime(true) * 1000), 'ts' => (int)(microtime(true) * 1000), 'text' => substr($t, 0, 1900), 'by' => 'claude'], JSON_UNESCAPED_UNICODE) . "\n", FILE_APPEND | LOCK_EX); };
    if ($st['limit']) { $post('Ghante ki had (15 messages) poori ho gayi, thori der baad likho.'); out(['ok' => true, 'limited' => true]); }
    require_once __DIR__ . '/agent.php';
    $post('Dekh raha hun...');
    try { $reply = ag_run((string)$CFG['anthropic_key'], (string)($CFG['anthropic_model'] ?? 'claude-sonnet-5-5'), (string)$cmd['text']); }
    catch (Throwable $e) { $reply = 'Agent error: ' . $e->getMessage(); }
    $post($reply); out(['ok' => true]);
}
case 'workerzip': { // latest worker.zip (Claude-edited version if it exists, else the repo one)
    $f = is_file("$DATA/worker_live.zip") ? "$DATA/worker_live.zip" : __DIR__ . '/worker.zip'; if (!is_file($f)) bad('worker.zip nahi', 404);
    header('Content-Type: application/zip'); header('Content-Disposition: attachment; filename="worker.zip"'); header('Content-Length: ' . filesize($f)); readfile($f); exit;
}
case 'doctor': {   // worker posts its doctor.py report at startup
    $j = json_decode(file_get_contents('php://input'), true); if (!is_array($j)) bad('doctor report samajh nahi aaya');
    $n = safe_id((string)($_GET['worker'] ?? 'worker')) ?: 'worker';
    jmod("$DATA/doctor.json", function ($x) use ($n, $j) { $x[$n] = ['ts' => time(), 'ok' => !empty($j['ok']), 'checks' => array_slice((array)($j['checks'] ?? []), 0, 80)]; return $x; });
    out(['ok' => true]);
}
case 'health': {   // System health: what THIS host can really do + the workers' doctor reports
    $dis = array_map('trim', explode(',', (string)ini_get('disable_functions')));
    $can = fn(string $f) => function_exists($f) && !in_array($f, $dis, true);
    $exec = $can('shell_exec') || $can('exec') || $can('proc_open');
    $bins = []; foreach (['pdftoppm', 'pdftotext', 'tesseract', 'python3', 'gs', 'convert'] as $b) {
        $p = $can('shell_exec') ? trim((string)@shell_exec('command -v ' . escapeshellarg($b) . ' 2>/dev/null')) : ''; $bins[$b] = $p !== '' ? $p : null; }
    $free = @disk_free_space($DATA);
    $h = ['php' => PHP_VERSION, 'zip' => class_exists('ZipArchive'), 'curl' => function_exists('curl_init'), 'exec_allowed' => $exec, 'binaries' => $bins,
          'memory_limit' => ini_get('memory_limit'), 'max_execution_time' => ini_get('max_execution_time'), 'upload_max' => ini_get('upload_max_filesize'),
          'data_writable' => is_writable($DATA), 'disk_free_gb' => $free ? round($free / 1073741824, 1) : null, 'claude_key_set' => !empty($CFG['anthropic_key']),
          'heavy_parsing_on_this_host' => $exec && !empty($bins['pdftoppm']) && !empty($bins['tesseract'])];
    @file_put_contents("$DATA/health_hub.json", json_encode($h + ['ts' => time()]));
    $w = jload($WORK, []); $d = jload("$DATA/doctor.json", []); $now = time();
    foreach ($d as $k => $x) $d[$k]['online'] = isset($w[$k]) && $now - (int)($w[$k]['seen'] ?? 0) < 60;
    out(['ok' => true, 'hub' => $h, 'workers' => $d]);
}
case 'review': {   // human review overlay per job: GET ?id= ; POST {qid, action: approve|edit|reset, edit:{stem,options,correct_answer_id,explanation}}
    $id = safe_id((string)($_GET['id'] ?? '')); if ($id === '') bad('id chahiye'); $f = "$DATA/review"; if (!is_dir($f)) @mkdir($f, 0775, true); $f .= "/$id.json";
    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST') {
        $j = json_decode(file_get_contents('php://input'), true); $qid = safe_id((string)($j['qid'] ?? '')); $act = (string)($j['action'] ?? ''); if ($qid === '' || !in_array($act, ['approve', 'edit', 'reset'], true)) bad('qid/action galat');
        $e = []; if ($act === 'edit') { $in = (array)($j['edit'] ?? []); if (isset($in['stem'])) $e['stem'] = substr((string)$in['stem'], 0, 6000); if (isset($in['explanation'])) $e['explanation'] = substr((string)$in['explanation'], 0, 20000);
            if (isset($in['correct_answer_id'])) $e['correct_answer_id'] = substr(preg_replace('/[^a-h]/', '', strtolower((string)$in['correct_answer_id'])), 0, 1);
            if (isset($in['options']) && is_array($in['options'])) { $e['options'] = []; foreach (array_slice($in['options'], 0, 10) as $o) $e['options'][] = ['id' => substr(preg_replace('/[^a-h]/', '', strtolower((string)($o['id'] ?? ''))), 0, 1), 'text' => substr((string)($o['text'] ?? ''), 0, 2000), 'percentage' => is_numeric($o['percentage'] ?? null) ? (float)$o['percentage'] : null]; } }
        jmod($f, function ($x) use ($qid, $act, $e) { if ($act === 'reset') unset($x[$qid]); else $x[$qid] = ['status' => $act === 'approve' ? 'approved_by_human' : 'edited_by_human', 'edit' => $e, 'ts' => time()]; return $x; });
    }
    out(['ok' => true, 'review' => jload($f, new stdClass())]);
}
case 'final': {    // student_view.json from the result zip merged with the human review overlay
    $id = safe_id((string)($_GET['id'] ?? '')); $z = new ZipArchive(); if (!is_file("$DATA/results/$id.zip") || $z->open("$DATA/results/$id.zip") !== true) bad('result nahi', 404);
    $raw = $z->getFromName('student_view.json'); $z->close(); $v = json_decode((string)$raw, true); if (!is_array($v)) bad('student_view.json nahi mili', 404);
    $rv = jload("$DATA/review/$id.json", []);
    foreach ($v['questions'] as &$q) { $r = $rv[$q['id'] ?? ''] ?? null; if (!$r) continue; $q['machine_status'] = $q['status']; $q['status'] = $r['status'];
        foreach ((array)($r['edit'] ?? []) as $k => $val) $q[$k] = $val; } unset($q);
    header('Content-Type: application/json; charset=utf-8'); header('Content-Disposition: attachment; filename="student_view_final.json"'); echo json_encode($v, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT); exit;
}
case 'setworker': { // bridge uploads the Claude-edited worker.zip; only whitelisted python/script names, backup kept, logged
    $tmp = "$DATA/worker_upload.tmp"; $n = body_to($tmp); if ($n < 1000 || $n > 8 * 1048576) { @unlink($tmp); bad('zip size galat'); }
    $z = new ZipArchive(); if ($z->open($tmp) !== true) { @unlink($tmp); bad('zip khula nahi'); }
    $ok = '#^(worker\.py|run_lab\.py|m2\.py|html_parse\.py|doctor\.py|make_sample\.py|setup_linux\.sh|lab/[a-z_]+\.py)$#'; $have = [];
    for ($i = 0; $i < $z->numFiles; $i++) { $nm = (string)$z->getNameIndex($i); if (substr($nm, -1) === '/') continue; if (!preg_match($ok, $nm)) { $z->close(); @unlink($tmp); bad('zip mein allowed na hone wali file: ' . $nm); } $have[$nm] = 1; }
    $z->close(); foreach (['worker.py', 'm2.py', 'run_lab.py', 'lab/pipeline.py', 'lab/verify.py'] as $r) if (!isset($have[$r])) { @unlink($tmp); bad("zip mein $r nahi hai"); }
    if (is_file("$DATA/worker_live.zip")) @copy("$DATA/worker_live.zip", "$DATA/worker_prev.zip");
    rename($tmp, "$DATA/worker_live.zip");
    @file_put_contents("$DATA/worker_uploads.log", date('c') . " $n bytes sha1=" . sha1_file("$DATA/worker_live.zip") . " files=" . count($have) . "\n", FILE_APPEND | LOCK_EX);
    out(['ok' => true, 'bytes' => $n, 'files' => count($have)]);
}
case 'rollbackworker': { // go back to the previous worker zip (or the repo's original)
    if (is_file("$DATA/worker_prev.zip")) { rename("$DATA/worker_prev.zip", "$DATA/worker_live.zip"); out(['ok' => true, 'to' => 'previous']); }
    @unlink("$DATA/worker_live.zip"); out(['ok' => true, 'to' => 'repo original']);
}
default: bad('a=? samajh nahi aaya', 404);
}
