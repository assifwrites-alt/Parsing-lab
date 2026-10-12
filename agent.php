<?php
// Dr Harry Lab - Claude agent that runs ON the hub (no Colab/Termux needed).
// Needs 'anthropic_key' in config.php. Claude may only read hub data and edit m2.py / worker.py / html_parse.py
// inside data/src. Every edit is backed up and logged to data/edits.log, and data/worker_live.zip is rebuilt.
// Included by api.php after token auth; direct access is blocked.
if (!isset($DATA)) { http_response_code(403); exit; }

const AG_FILES = ['m2.py', 'worker.py', 'html_parse.py', 'run_lab.py', 'doctor.py'];

function ag_src(): string { global $DATA; $d = "$DATA/src"; if (!is_dir($d)) @mkdir($d, 0775, true); return $d; }
function ag_seed(): void {   // copy files from the repo's worker.zip that are not in data/src yet; never overwrites Claude's edits
    $src = ag_src(); $z = new ZipArchive(); $zip = __DIR__ . '/worker.zip';
    if (!is_file($zip) || $z->open($zip) !== true) return;
    for ($i = 0; $i < $z->numFiles; $i++) {
        $n = $z->getNameIndex($i); if (substr($n, -1) === '/' || strpos($n, '..') !== false || is_file("$src/$n")) continue;
        @mkdir(dirname("$src/$n"), 0775, true); file_put_contents("$src/$n", $z->getFromIndex($i));
    }
    $z->close();
}
function ag_rebuild_zip(): void {
    global $DATA; $src = ag_src(); $out = "$DATA/worker_live.zip"; $tmp = "$out.tmp";
    $z = new ZipArchive(); if ($z->open($tmp, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) return;
    foreach (['worker.py', 'run_lab.py', 'm2.py', 'html_parse.py', 'doctor.py', 'make_sample.py', 'setup_linux.sh'] as $f) if (is_file("$src/$f")) $z->addFile("$src/$f", $f);
    foreach (glob("$src/lab/*.py") ?: [] as $f) $z->addFile($f, 'lab/' . basename($f));
    $z->close(); @rename($tmp, $out);
}
function ag_pycheck(string $file): ?string {   // null = ok or unable to check; string = syntax error text
    if (!function_exists('shell_exec') || stripos((string)ini_get('disable_functions'), 'shell_exec') !== false) return null;
    $o = @shell_exec('python3 -c ' . escapeshellarg('import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read());print("OK")') . ' ' . escapeshellarg($file) . ' 2>&1');
    if ($o === null || $o === false || $o === '') return null;
    return strpos($o, 'OK') === 0 ? null : substr($o, -600);
}
function ag_file(string $n): ?string {
    if (in_array($n, AG_FILES, true) || preg_match('#^lab/[a-z_]+\.py$#', $n)) return ag_src() . '/' . $n;
    return null;
}

function ag_tool(string $name, array $in, array &$edited): string {
    global $DATA, $JOBS;
    switch ($name) {
    case 'list_jobs':
        $j = jload($JOBS, []); uasort($j, fn($x, $y) => ($y['updated'] ?? 0) <=> ($x['updated'] ?? 0)); $r = [];
        foreach (array_slice($j, 0, 6) as $x) $r[] = array_intersect_key($x, array_flip(['id', 'name', 'size', 'status', 'pct', 'stage', 'detail', 'worker', 'created', 'updated']));
        return json_encode($r, JSON_UNESCAPED_UNICODE);
    case 'read_log':
        $id = safe_id((string)($in['id'] ?? '')); $f = "$DATA/logs/$id.log";
        return is_file($f) ? substr((string)file_get_contents($f), -7000) : 'is job ka log nahi hai';
    case 'read_file':
        $f = ag_file((string)($in['file'] ?? '')); if (!$f || !is_file($f)) return 'file allowed nahi ya nahi mili';
        $L = file($f, FILE_IGNORE_NEW_LINES); $s = max(1, (int)($in['start'] ?? 1)); $e = min(count($L), (int)($in['end'] ?? $s + 120), $s + 249);
        $o = ''; for ($i = $s; $i <= $e; $i++) $o .= $i . ': ' . $L[$i - 1] . "\n";
        return "(total " . count($L) . " lines)\n" . $o;
    case 'grep':
        $f = ag_file((string)($in['file'] ?? '')); if (!$f || !is_file($f)) return 'file allowed nahi ya nahi mili';
        $p = (string)($in['text'] ?? ''); if ($p === '') return 'text khali'; $o = ''; $c = 0;
        foreach (file($f, FILE_IGNORE_NEW_LINES) as $i => $l) if (stripos($l, $p) !== false) { $o .= ($i + 1) . ': ' . substr($l, 0, 220) . "\n"; if (++$c >= 40) break; }
        return $o ?: 'koi match nahi';
    case 'replace':
        $f = ag_file((string)($in['file'] ?? '')); if (!$f || !is_file($f)) return 'file allowed nahi ya nahi mili';
        $old = (string)($in['old'] ?? ''); $new = (string)($in['new'] ?? ''); if ($old === '') return 'old khali';
        $s = (string)file_get_contents($f); $cnt = substr_count($s, $old);
        if ($cnt !== 1) return "old text $cnt jagah mila; ek hi jagah milna chahiye, thora lamba old text do";
        $bk = ag_src() . '/.bak'; if (!is_dir($bk)) @mkdir($bk, 0775, true);
        @mkdir(dirname($f), 0775, true);
        copy($f, $bk . '/' . str_replace('/', '_', ($in['file'] ?? 'f')) . '.' . date('Ymd-His'));
        file_put_contents($f, str_replace($old, $new, $s));
        if (substr($f, -3) === '.py') { $err = ag_pycheck($f); if ($err !== null) { file_put_contents($f, $s); return "syntax error, edit wapas le liya:\n$err"; } }
        $edited[(string)$in['file']] = true;
        @file_put_contents("$DATA/edits.log", "\n=== " . date('Y-m-d H:i:s') . ' ' . $in['file'] . "\n--- OLD\n$old\n+++ NEW\n$new\n", FILE_APPEND | LOCK_EX);
        return 'edit ho gaya';
    }
    return 'unknown tool';
}

function ag_api(string $key, array $body): array {
    global $CFG;
    $ch = curl_init((string)($CFG['anthropic_url'] ?? 'https://api.anthropic.com/v1/messages'));
    curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 120,
        CURLOPT_HTTPHEADER => ['content-type: application/json', 'x-api-key: ' . $key, 'anthropic-version: 2023-06-01'],
        CURLOPT_POSTFIELDS => json_encode($body, JSON_UNESCAPED_UNICODE)]);
    $r = curl_exec($ch); $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE); curl_close($ch);
    $j = $r ? json_decode((string)$r, true) : null;
    if ($code !== 200 || !is_array($j)) return ['error' => 'API ' . $code . ': ' . substr((string)$r, 0, 300)];
    return $j;
}

function ag_run(string $key, string $model, string $text): string {
    ag_seed();
    $obj = ['type' => 'object'];
    $T = [
      ['name' => 'list_jobs', 'description' => 'Latest jobs with status/progress/error.', 'input_schema' => $obj + ['properties' => new stdClass()]],
      ['name' => 'read_log', 'description' => 'Worker diagnostics log for a job id (probe info + end of run.log).', 'input_schema' => $obj + ['properties' => ['id' => ['type' => 'string']], 'required' => ['id']]],
      ['name' => 'read_file', 'description' => 'Read lines of an editable file e.g. m2.py, worker.py, lab/verify.py (max 250 lines per call).', 'input_schema' => $obj + ['properties' => ['file' => ['type' => 'string'], 'start' => ['type' => 'integer'], 'end' => ['type' => 'integer']], 'required' => ['file']]],
      ['name' => 'grep', 'description' => 'Case-insensitive literal search in one of the editable files.', 'input_schema' => $obj + ['properties' => ['file' => ['type' => 'string'], 'text' => ['type' => 'string']], 'required' => ['file', 'text']]],
      ['name' => 'replace', 'description' => 'Replace one exact, unique text block in m2.py/worker.py/html_parse.py. Syntax is checked; bad edits are reverted. Keep edits minimal.', 'input_schema' => $obj + ['properties' => ['file' => ['type' => 'string'], 'old' => ['type' => 'string'], 'new' => ['type' => 'string']], 'required' => ['file', 'old', 'new']]],
    ];
    $sys = "Tum Dr Harry Lab ke maintainer ho: ek PDF parsing hub (PHP) aur Colab worker. worker.py PDF par m2.py chalata hai (amedex = phone screenshots OCR, warna text-layer mode). "
         . "User Roman English mein likhta hai; jawab bhi chhota Roman English mein do. Pehle list_jobs/read_log se asli wajah dhoondho, phir sirf zarurat par minimal replace karo. "
         . "Sirf m2.py, worker.py, html_parse.py, run_lab.py, doctor.py aur lab/*.py edit ho sakti hain (lab/ = verify, images, vision_fix, report, pipeline). Kuch aur karne ko kaha jaye to mana karo. Edit ke baad batao kya badla aur ye ke user Colab ka Cell 1 dobara chalaye (naya worker.zip) aur Dobara try dabaye.";
    $msgs = [['role' => 'user', 'content' => $text]]; $edited = []; $t0 = time(); $final = '';
    for ($turn = 0; $turn < 14 && time() - $t0 < 240; $turn++) {
        $r = ag_api($key, ['model' => $model, 'max_tokens' => 2000, 'system' => $sys, 'tools' => $T, 'messages' => $msgs]);
        if (isset($r['error'])) { $final = 'Claude API error: ' . $r['error']; break; }
        $msgs[] = ['role' => 'assistant', 'content' => $r['content']];
        $res = [];
        foreach ($r['content'] as $b) {
            if (($b['type'] ?? '') === 'text') $final = $b['text'];
            if (($b['type'] ?? '') === 'tool_use') $res[] = ['type' => 'tool_result', 'tool_use_id' => $b['id'], 'content' => substr(ag_tool($b['name'], (array)($b['input'] ?? []), $edited), 0, 12000)];
        }
        if (!$res) break;
        $msgs[] = ['role' => 'user', 'content' => $res];
    }
    if ($edited) { ag_rebuild_zip(); $final .= "\n\n[Badli hui files: " . implode(', ', array_keys($edited)) . ". Naya worker.zip tayyar: Colab Cell 1 dobara chalao.]"; }
    return trim($final) ?: '(koi jawab nahi bana)';
}
