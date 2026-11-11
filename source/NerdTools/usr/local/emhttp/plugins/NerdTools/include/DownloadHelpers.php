<?php
class NerdToolsHttpError extends RuntimeException {
    public $reset;
    public function __construct($message, $reset = 0) { parent::__construct($message); $this->reset = $reset; }
}
function nt_http($url, $file = null, $token = '') {
    $allowed = strpos($url, 'https://api.github.com/repos/andrebrait/unRAID-NerdTools/releases?') === 0
        || strpos($url, 'https://github.com/andrebrait/unRAID-NerdTools/releases/download/') === 0;
    if (!$allowed) throw new RuntimeException('Refusing untrusted download URL');
    $headers = []; $body = ''; $bytes = 0;
    $stream = $file === null ? null : fopen($file, 'wb');
    if ($file !== null && !$stream) throw new RuntimeException('Cannot write download');
    $curl = curl_init($url);
    curl_setopt_array($curl, [
        CURLOPT_FOLLOWLOCATION=>true, CURLOPT_MAXREDIRS=>5,
        CURLOPT_PROTOCOLS=>CURLPROTO_HTTPS, CURLOPT_REDIR_PROTOCOLS=>CURLPROTO_HTTPS,
        CURLOPT_CONNECTTIMEOUT=>15, CURLOPT_TIMEOUT=>$file === null ? 90 : 1800,
        CURLOPT_SSL_VERIFYPEER=>true, CURLOPT_SSL_VERIFYHOST=>2,
        CURLOPT_USERAGENT=>'NerdTools/7.3 (andrebrait/unRAID-NerdTools)',
        // The token goes only to the API host; asset downloads never carry it (curl also drops it on redirects).
        CURLOPT_HTTPHEADER=>array_merge(['Accept: application/vnd.github+json', 'X-GitHub-Api-Version: 2022-11-28'],
            $token !== '' && strpos($url, 'https://api.github.com/') === 0 ? ['Authorization: Bearer '.$token] : []),
        CURLOPT_HEADERFUNCTION=>function($curl, $line) use (&$headers) {
            if (strpos($line, 'HTTP/') === 0) $headers = [];
            $parts = explode(':', $line, 2);
            if (count($parts) === 2) $headers[strtolower(trim($parts[0]))] = trim($parts[1]);
            return strlen($line);
        },
        CURLOPT_WRITEFUNCTION=>function($curl, $chunk) use ($stream, &$body, &$bytes) {
            $length = strlen($chunk); $bytes += $length;
            if ($bytes > ($stream ? 2147483648 : 16777216)) return 0;
            if ($stream) return fwrite($stream, $chunk);
            $body .= $chunk; return $length;
        },
    ]);
    try {
        $ok = curl_exec($curl); $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
        $reset = 0;
        if ($status === 403 || $status === 429) {
            if (isset($headers['x-ratelimit-reset'])) $reset = (int)$headers['x-ratelimit-reset'];
            if (isset($headers['retry-after'])) {
                $retry = ctype_digit($headers['retry-after']) ? time() + (int)$headers['retry-after'] : strtotime($headers['retry-after']);
                $reset = max($reset, $retry ?: 0);
            }
        }
        if ($ok === false || $status !== 200) {
            throw new NerdToolsHttpError('Download failed'.($status ? ' (HTTP '.$status.')' : ': '.curl_error($curl)), $reset);
        }
        return ['body'=>$body, 'headers'=>$headers];
    } finally { curl_close($curl); if ($stream) fclose($stream); }
}
