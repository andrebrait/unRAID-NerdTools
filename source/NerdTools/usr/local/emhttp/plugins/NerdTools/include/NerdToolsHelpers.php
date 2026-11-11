<?php
require_once __DIR__.'/DownloadHelpers.php';

const NT_REPOSITORY = 'andrebrait/unRAID-NerdTools';
const NT_MIN_UNRAID = '7.3.4';
// BlueZ 5.88 source SHA256 6952c39eb5f8b0ee712cc50ab3bcff03be17c410dd97ca640f4849d9a1653ba3; only the audited helper-path rewrite is applied.
const NT_BLUEZ_HID2HCI_SHA256 = '8d4ed90eae12d0c0cac3c65c1960fb90c3f3fde264696bbe30f590a8cd4aa2ed';
// Immutable Slackware source/ap/screen/screen.pam at 8dbbc18b2042228d0189b2ddf267c1028c860f61; requires pam_securetty and host system-auth.
const NT_SCREEN_PAM_SHA256 = '4e631aaf91babb312ec39a09d6cfd6e0a57d8ac5a48eb58a9bade25af3a937aa';

// Stable releases, betas and release candidates; a family is usable only once a catalog was published for it.
function nt_supported($version) {
    return is_string($version) && preg_match('/^\d+\.\d+\.\d+(-(beta|rc)\.?\d+)?$/D', $version) && version_compare($version, NT_MIN_UNRAID, '>=');
}
function nt_family($version) { return implode('.', array_slice(explode('.', $version), 0, 2)); }
function nt_atomic_write($path, $contents) {
    if (is_link($path) || is_link(dirname($path))) throw new RuntimeException('Refusing linked state path');
    $tmp = tempnam(dirname($path), '.nerdtools-');
    if ($tmp === false) throw new RuntimeException('Cannot create temporary state');
    try {
        if (file_put_contents($tmp, $contents) !== strlen($contents) || !rename($tmp, $path)) {
            throw new RuntimeException('Cannot save NerdTools state');
        }
    } finally { if (file_exists($tmp)) unlink($tmp); }
}
function nt_json($path, $default = []) {
    if (is_link($path)) throw new RuntimeException('Refusing linked NerdTools state');
    if (!is_file($path)) return $default;
    $value = json_decode(file_get_contents($path), true);
    if (!is_array($value)) throw new RuntimeException('Invalid NerdTools state: '.basename($path));
    return $value;
}
function nt_name($name) {
    return is_string($name) && preg_match('/^[A-Za-z0-9][A-Za-z0-9+_.-]*$/D', $name);
}
function nt_asset_url($url) {
    return is_string($url) && strpos($url, 'https://github.com/'.NT_REPOSITORY.'/releases/download/') === 0
        && !preg_match('/[\x00-\x20]/', $url);
}
function nt_resolve($selected, $packages) {
    $result = []; $visiting = []; $visited = [];
    $visit = function($name) use (&$visit, &$result, &$visiting, &$visited, $packages) {
        if (!isset($packages[$name])) throw new RuntimeException('Missing dependency or selected package: '.$name);
        if (isset($visiting[$name])) throw new RuntimeException('Dependency cycle: '.$name);
        if (isset($visited[$name])) return;
        $visiting[$name] = true;
        foreach ($packages[$name]['dependencies'] as $dependency) $visit($dependency);
        unset($visiting[$name]); $visited[$name] = true; $result[] = $name;
    };
    foreach ($selected as $name) $visit($name);
    return $result;
}
function nt_validate_catalog($catalog, $assets, $version) {
    if (!nt_supported($version) || !is_array($catalog) || ($catalog['family'] ?? null) !== nt_family($version)
        || !nt_supported($catalog['min_unraid'] ?? null) || version_compare($version, $catalog['min_unraid'], '<')
        || !isset($catalog['packages']) || !is_array($catalog['packages']) || !array_is_list($catalog['packages'])) {
        throw new RuntimeException('Catalog is not compatible with this Unraid version');
    }
    $byName = [];
    foreach ($assets as $asset) {
        if (!isset($asset['name']) || isset($byName[$asset['name']])) throw new RuntimeException('Ambiguous release assets');
        $byName[$asset['name']] = $asset;
    }
    $packages = []; $commandOwners = [];
    foreach ($catalog['packages'] as $package) {
        if (!is_array($package) || !nt_name($package['name'] ?? null) || isset($packages[$package['name']])
            || !is_string($package['version'] ?? null) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9+_.]*$/D', $package['version'])
            || !is_string($package['file'] ?? null)
            || !preg_match('/^'.preg_quote($package['name'].'-'.$package['version'], '/').'-(?:x86_64|noarch)-[A-Za-z0-9+_.]+\.txz$/D', $package['file'])
            || !is_string($package['sha256'] ?? null) || !preg_match('/^[a-fA-F0-9]{64}$/D', $package['sha256'])
            || !is_int($package['size'] ?? null) || $package['size'] < 1
            || (isset($package['internal']) && !is_bool($package['internal']))
            || !isset($package['dependencies']) || !is_array($package['dependencies']) || !array_is_list($package['dependencies'])) {
            throw new RuntimeException('Invalid package in catalog');
        }
        foreach ($package['dependencies'] as $dependency) if (!nt_name($dependency)) throw new RuntimeException('Invalid dependency');
        $asset = $byName[$package['file']] ?? [];
        if (($asset['size'] ?? null) !== $package['size'] || !nt_asset_url($asset['browser_download_url'] ?? null)) {
            throw new RuntimeException('Missing or invalid release asset: '.$package['file']);
        }
        $package['url'] = $asset['browser_download_url'];
        $tool = $package['tool'] ?? nt_tool_name($package['name']);
        if (is_string($tool) && strcasecmp($tool, 'rar') === 0) throw new RuntimeException('RAR must remain external; native redistribution is not supported');
        if (!nt_name($tool) || (isset($package['tool']) && $package['name'] !== 'nerdtools-'.$tool)) throw new RuntimeException('Invalid package identity');
        $commands = $package['commands'] ?? [];
        if (!is_array($commands) || !array_is_list($commands)) throw new RuntimeException('Invalid command metadata');
        foreach ($commands as $command) {
            if (is_string($command) && strcasecmp($command, 'rar') === 0) throw new RuntimeException('RAR must remain external; native redistribution is not supported');
            if (!nt_name($command) || strpos($command, 'nerdtools-') === 0 || isset($commandOwners[$command])) throw new RuntimeException('Ambiguous or invalid package command');
            $commandOwners[$command] = $package['name'];
        }
        $package['tool'] = $tool; $package['commands'] = $commands;
        $packages[$package['name']] = $package;
    }
    nt_resolve(array_keys($packages), $packages);
    $external = $catalog['external_packages'] ?? [];
    if (!is_array($external) || !array_is_list($external)) throw new RuntimeException('Invalid external offers');
    foreach ($external as $offer) {
        if (!is_array($offer) || ($offer['name'] ?? null) !== 'rar' || isset($packages['rar'])
            || ($offer['delivery'] ?? null) !== 'external'
            || !is_string($offer['version'] ?? null) || !preg_match('/^\d+\.\d+$/D', $offer['version'])
            || !is_string($offer['url'] ?? null) || !preg_match('#^https://www\.rarlab\.com/rar/rarlinux-x64-\d+\.tar\.gz$#D', $offer['url'])
            || ($offer['license_url'] ?? null) !== 'https://www.rarlab.com/license.htm'
            || !is_string($offer['license_notice'] ?? null) || strlen($offer['license_notice']) > 2000) {
            throw new RuntimeException('Invalid external software offer');
        }
    }
    $catalog['external_packages'] = $external;
    $provided = $catalog['provided_packages'] ?? []; $seen = [];
    if (!is_array($provided) || !array_is_list($provided)) throw new RuntimeException('Invalid built-in capabilities');
    foreach ($provided as $capability) {
        if (!is_array($capability) || !nt_name($capability['name'] ?? null) || isset($seen[$capability['name']]) || isset($packages[$capability['name']])
            || ($capability['provider'] ?? null) !== 'unraid' || !nt_supported($capability['min_unraid'] ?? null)
            || version_compare($version, $capability['min_unraid'], '<')
            || !is_string($capability['version'] ?? null) || !preg_match('/^[A-Za-z0-9][A-Za-z0-9+_.-]*$/D', $capability['version'])
            || isset($capability['file']) || isset($capability['url']) || isset($capability['sha256'])) {
            throw new RuntimeException('Invalid built-in capability');
        }
        $seen[$capability['name']] = true;
    }
    $catalog['provided_packages'] = $provided;
    $catalog['packages'] = $packages;
    return $catalog;
}
function nt_preference($config, $name) {
    foreach (array_unique([$name, nt_tool_name($name)]) as $identity) {
        if (isset($config[$identity])) return $config[$identity] === 'yes' ? 'yes' : 'no';
        foreach ($config as $key => $value) {
            if (preg_match('/^'.preg_quote($identity, '/').'-[0-9][A-Za-z0-9_+.]*$/D', $key)) return $value === 'yes' ? 'yes' : 'no';
        }
    }
    return 'no';
}
function nt_next_check($now, $hours, $failed, $reset) {
    return max($now + ($failed ? 3600 : $hours * 3600), $reset);
}
function nt_verified($file, $package) {
    return is_file($file) && !is_link($file) && filesize($file) === $package['size']
        && hash_equals(strtolower($package['sha256']), hash_file('sha256', $file));
}
function nt_package_name($record) {
    return preg_match('/^(.+)-[^-]+-(?:x86_64|noarch|i[3-6]86)-[^-]+$/D', $record, $match) ? $match[1] : null;
}
function nt_record($file) { return substr($file, 0, -4); }
function nt_tool_name($name) { return strpos($name, 'nerdtools-') === 0 ? substr($name, 10) : $name; }
function nt_log($message, $quiet = false) {
    openlog('NerdTools', LOG_PID, LOG_USER); syslog(LOG_INFO, $message); closelog();
    if (!$quiet) echo $message."\n";
}
function format_size($value, $decimals = 1) {
    foreach (['TB'=>1099511627776, 'GB'=>1073741824, 'MB'=>1048576, 'KB'=>1024] as $unit => $size) {
        if ($value >= $size) return number_format($value / $size, $decimals).' '.$unit;
    }
    return $value.' B';
}

function nt_payload_allowed($tool, $path, $type, $permissions, $uid, $gid) {
    if ($uid !== 0 || strlen($permissions) !== 9) return false;
    if (in_array($tool, ['mlocate','plocate'], true)) {
        if ($path === 'opt/nerdtools/bin/'.$tool) return $type === '-' && $gid === 21 && $permissions === 'rwx--s--x';
        if ($path === 'opt/nerdtools/var/lib/'.$tool) return $type === 'd' && $gid === 21 && $permissions === 'rwxr-x---';
    }
    if ($gid !== 0 || in_array($permissions[2], ['s','S'], true) || in_array($permissions[5], ['s','S'], true)
        || in_array($permissions[8], ['t','T'], true) || ($type !== 'l' && $permissions[7] === 'w')) return false;
    if ($path === '' || in_array($path, ['opt','opt/nerdtools','usr','usr/local','usr/local/bin','install'], true)) {
        return $type === 'd' && $permissions === 'rwxr-xr-x';
    }
    if (strpos($path, 'opt/nerdtools/') === 0) return in_array($type, ['-','d','l','h'], true);
    if (preg_match('#^usr/local/bin/nerdtools-[A-Za-z0-9_+.-]+$#D', $path)) return $type === '-' && $permissions === 'rwxr-xr-x';
    if ($path === 'install/slack-desc' || $path === 'install/doinst.sh') {
        return $type === '-' && in_array($permissions, ['rw-r--r--','rwxr-xr-x'], true);
    }
    $files = [];
    if ($tool === 'audit') $files = ['etc/rc.d/rc.auditd.new'];
    if ($tool === 'screen') $files = ['etc/screenrc.new','etc/skel/.screenrc.new','etc/pam.d/screen.new'];
    if ($tool === 'nmap') $files = ['usr/share/applications/nerdtools-zenmap.desktop'];
    if ($tool === 'bluez') {
        $files = ['etc/bluetooth/input.conf.new','etc/bluetooth/main.conf.new','etc/bluetooth/network.conf.new',
            'etc/bluetooth/mesh-main.conf.new',
            'etc/default/bluetooth.new','etc/rc.d/rc.bluetooth.new','etc/modprobe.d/btusb.conf',
            'etc/udev/rules.d/97-hid2hci.rules','etc/xdg/autostart/mpris-proxy.desktop',
            'usr/share/dbus-1/system.d/bluetooth.conf','usr/share/dbus-1/system.d/obex.conf',
            'usr/share/dbus-1/system.d/bluetooth-mesh.conf',
            'usr/share/dbus-1/services/org.bluez.obex.service'];
        if ($path === 'var/lib/bluetooth') return $type === 'd' && $permissions === 'rwx------';
        if (in_array($path, ['var','var/lib'], true)) return $type === 'd' && $permissions === 'rwxr-xr-x';
    }
    if (in_array($path, $files, true)) return $type === '-' && $permissions === 'rw-r--r--';
    if ($type !== 'd' || $permissions !== 'rwxr-xr-x') return false;
    foreach ($files as $file) {
        for ($parent = dirname($file); $parent !== '.'; $parent = dirname($parent)) if ($path === $parent) return true;
    }
    return false;
}

class NerdTools {
    private $root;
    private $transport;
    private $runner;
    public $path;
    public $version;
    public function __construct($root = '', $transport = null, $runner = null) {
        $this->root = rtrim($root, '/');
        $this->transport = $transport;
        $this->runner = $runner;
        $this->path = $this->root.'/boot/config/plugins/NerdTools';
        $ini = @parse_ini_file($this->root.'/etc/unraid-version', false, INI_SCANNER_RAW);
        $this->version = $ini['version'] ?? '';
        if (!nt_supported($this->version)) throw new RuntimeException('NerdTools requires Unraid 7.3.4 or later; unknown versions are not supported');
        foreach ([$this->path, $this->path.'/packages'] as $directory) {
            if (is_link($directory) || (!is_dir($directory) && !mkdir($directory, 0755, true))) {
                throw new RuntimeException('Cannot create safe NerdTools state directory');
            }
        }
    }
    public function lock() {
        $path = $this->path.'/manager.lock';
        if (is_link($path)) throw new RuntimeException('Unsafe lock path');
        $lock = fopen($path, 'c');
        if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) throw new RuntimeException('NerdTools is busy; try again later');
        return $lock;
    }
    private function fetch($url, $file = null) {
        return $this->transport ? call_user_func($this->transport, $url, $file) : nt_http($url, $file);
    }
    public function config() {
        $file = $this->path.'/NerdTools.cfg';
        $config = is_file($file) ? parse_ini_file($file, false, INI_SCANNER_RAW) : [];
        if (!is_array($config)) throw new RuntimeException('Cannot read NerdTools configuration');
        return $config;
    }
    public function interval() {
        $value = $this->config()['update_check_hours'] ?? '24';
        if (!ctype_digit((string)$value) || (int)$value > 8760) throw new RuntimeException('Invalid update-check interval');
        return (int)$value;
    }
    public function state() { return nt_json($this->path.'/check-state.json'); }
    public function catalog() {
        $cache = nt_json($this->path.'/catalog-cache.json');
        if (!$cache) return [];
        // Revalidate cached data after upgrades and before any install.
        $catalog = nt_validate_catalog($cache['catalog'] ?? null, $cache['assets'] ?? [], $this->version);
        $catalog['release'] = $cache['release'] ?? '';
        $catalog['selector'] = $cache['selector'] ?? 'latest';
        $catalog['releases'] = $cache['releases'] ?? [];
        return $catalog;
    }
    public function refresh($force = false, $selector = null) {
        $state = $this->state(); $now = time(); $hours = $this->interval();
        if ((!$force && (!$hours || $now < ($state['next_check'] ?? 0))) || $now < ($state['rate_limit_reset'] ?? 0)) return false;
        $cache = nt_json($this->path.'/catalog-cache.json');
        $selector = $selector ?? ($cache['selector'] ?? 'latest');
        if (!is_string($selector) || !preg_match('#^[A-Za-z0-9][A-Za-z0-9_./+-]{0,199}$#D', $selector)) throw new RuntimeException('Invalid release tag');
        $reset = 0;
        try {
            $release = null; $raw = null; $choices = [];
            // Keep published release history selectable; plugin-only and prerelease tags are isolated.
            for ($page = 1; ; $page++) {
                $response = $this->fetch('https://api.github.com/repos/'.NT_REPOSITORY.'/releases?per_page=100&page='.$page);
                $releases = json_decode($response['body'], true);
                if (!is_array($releases) || !array_is_list($releases)) throw new RuntimeException('Invalid GitHub Releases response');
                foreach ($releases as $candidate) {
                    if (!is_array($candidate) || !empty($candidate['draft']) || !empty($candidate['prerelease'])
                        || !is_string($candidate['tag_name'] ?? null)) continue;
                    foreach (($candidate['assets'] ?? []) as $asset) {
                        if (($asset['name'] ?? '') !== 'catalog-'.nt_family($this->version).'.json') continue;
                        $choices[] = $candidate['tag_name'];
                        if ($release || ($selector !== 'latest' && $candidate['tag_name'] !== $selector)) break;
                        if (!nt_asset_url($asset['browser_download_url'] ?? null)) throw new RuntimeException('Invalid catalog asset URL');
                        $response = $this->fetch($asset['browser_download_url']);
                        $data = json_decode($response['body'], true);
                        if ($selector === 'latest' && nt_supported($data['min_unraid'] ?? null)
                            && version_compare($this->version, $data['min_unraid'], '<')) break;
                        nt_validate_catalog($data, $candidate['assets'], $this->version);
                        $release = $candidate; $raw = $data;
                        break;
                    }
                }
                if (count($releases) < 100) break;
            }
            if (!$release) throw new RuntimeException('No compatible published package release found'.($selector === 'latest' ? '' : ': '.$selector));
            nt_atomic_write($this->path.'/catalog-cache.json', json_encode([
                'catalog'=>$raw, 'assets'=>$release['assets'], 'release'=>$release['tag_name'],
                'selector'=>$selector, 'releases'=>$choices,
            ], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES));
            $state = ['last_success'=>$now, 'last_attempt'=>$now, 'next_check'=>nt_next_check($now, $hours ?: 24, false, 0), 'error'=>'', 'rate_limit_reset'=>0];
        } catch (Throwable $error) {
            if ($error instanceof NerdToolsHttpError) $reset = $error->reset;
            $state = array_merge($state, ['last_attempt'=>$now, 'next_check'=>nt_next_check($now, $hours ?: 24, true, $reset),
                'error'=>$error->getMessage(), 'rate_limit_reset'=>$reset]);
            nt_atomic_write($this->path.'/check-state.json', json_encode($state, JSON_THROW_ON_ERROR));
            throw $error;
        }
        nt_atomic_write($this->path.'/check-state.json', json_encode($state, JSON_THROW_ON_ERROR));
        return true;
    }
    public function save($preferences, $hours) {
        if (!is_array($preferences) || !is_scalar($hours) || !ctype_digit((string)$hours) || (int)$hours > 8760) {
            throw new RuntimeException('Invalid settings');
        }
        $catalog = $this->catalog(); $packages = $catalog['packages'] ?? []; $owned = $this->legacyOwned();
        $this->saveOwned($owned);
        $config = $this->config();
        foreach ($preferences as $name => $value) {
            if ((!isset($packages[$name]) && !isset($owned[$name])) || !in_array($value, ['yes','no'], true)) throw new RuntimeException('Invalid package preference');
            // Migrate only this tool's old stable/version keys; unrelated preferences survive.
            foreach (array_unique([$name, nt_tool_name($name)]) as $identity) foreach (array_keys($config) as $key) {
                if (($identity !== $name && $key === $identity) || preg_match('/^'.preg_quote($identity, '/').'-[0-9][A-Za-z0-9_+.]*$/D', $key)) unset($config[$key]);
            }
            $config[$name] = $value;
        }
        $config['update_check_hours'] = (string)(int)$hours;
        $contents = '';
        foreach ($config as $name => $value) {
            if (!preg_match('/^[A-Za-z0-9_+.-]+$/D', (string)$name) || !is_scalar($value)) throw new RuntimeException('Invalid existing configuration; nothing was saved');
            $contents .= $name.'="'.addcslashes((string)$value, "\\\"").'"'."\n";
        }
        nt_atomic_write($this->path.'/NerdTools.cfg', $contents);
        // Changing the normal interval must not postpone a failed check's hourly retry.
        $state = $this->state();
        $state['next_check'] = empty($state['error'])
            ? max($state['rate_limit_reset'] ?? 0, ($state['last_success'] ?? 0) + (int)$hours * 3600)
            : max($state['rate_limit_reset'] ?? 0, $state['next_check'] ?? (($state['last_attempt'] ?? time()) + 3600));
        nt_atomic_write($this->path.'/check-state.json', json_encode($state, JSON_THROW_ON_ERROR));
        $this->schedule();
    }
    public function installed() {
        $directory = $this->root.'/var/log/packages';
        return is_dir($directory) ? array_values(array_diff(scandir($directory), ['.','..'])) : [];
    }
    public function owned() {
        $owned = nt_json($this->path.'/owned-packages.json');
        foreach ($owned as $name => $entry) {
            if (!nt_name($name) || !is_array($entry) || !isset($entry['file'],$entry['record'])
                || !nt_name($entry['file']) || substr($entry['file'], -4) !== '.txz'
                || nt_record($entry['file']) !== $entry['record'] || nt_package_name($entry['record']) !== $name
                || (!empty($entry['legacy']) && substr($entry['record'], -10) !== '_nerdtools')
                || !isset($entry['dependencies']) || !is_array($entry['dependencies'])) {
                throw new RuntimeException('Invalid package ownership state');
            }
            if ((isset($entry['tool']) && !nt_name($entry['tool'])) || (isset($entry['commands']) && (!is_array($entry['commands']) || !array_is_list($entry['commands'])))) {
                throw new RuntimeException('Invalid owned command metadata');
            }
            foreach (($entry['commands'] ?? []) as $command) if (!nt_name($command)) throw new RuntimeException('Invalid owned command metadata');
        }
        return $owned;
    }
    private function saveOwned($owned) {
        nt_atomic_write($this->path.'/owned-packages.json', json_encode($owned, JSON_THROW_ON_ERROR | JSON_FORCE_OBJECT));
    }
    private function command($arguments, $capture = false) {
        if ($this->runner) {
            $result = call_user_func($this->runner, $arguments, $capture);
            return $capture === 'raw' && is_array($result) ? implode("\n", $result)."\n" : $result;
        }
        if ($this->root !== '') throw new RuntimeException('Host commands are disabled for isolated roots');
        if ($capture) {
            $process = proc_open($arguments, [0=>['file','/dev/null','r'],1=>['pipe','w'],2=>['file','/dev/null','a']], $pipes, null,
                ['LC_ALL'=>'C','PATH'=>'/usr/sbin:/usr/bin:/sbin:/bin']);
            if (!is_resource($process)) throw new RuntimeException('Cannot inspect package archive');
            $contents = stream_get_contents($pipes[1], 16777217);
            if (strlen($contents) > 16777216) proc_terminate($process);
            fclose($pipes[1]); $status = proc_close($process);
            if ($status !== 0 || strlen($contents) > 16777216) throw new RuntimeException('Package archive inspection failed');
            return $capture === 'raw' ? $contents : ($contents === '' ? [] : explode("\n", rtrim($contents, "\n")));
        }
        $command = implode(' ', array_map('escapeshellarg', $arguments));
        passthru($command.' 2>&1', $status);
        if ($status !== 0) throw new RuntimeException(basename($arguments[0]).' failed (exit '.$status.')');
        return [];
    }
    private function packageFiles($record) {
        $log = $this->root.'/var/log/packages/'.$record;
        if (!is_file($log) || is_link($log)) throw new RuntimeException('Missing installed package record: '.$record);
        $contents = file_get_contents($log);
        $offset = strpos($contents, "FILE LIST:\n");
        if ($offset === false) throw new RuntimeException('Invalid installed package record: '.$record);
        return array_fill_keys(explode("\n", substr($contents, $offset + 11)), true);
    }
    private function safeArchive($file, $oldRecord, &$claims) {
        $oldFiles = $oldRecord ? $this->packageFiles($oldRecord) : [];
        $records = []; $configs = [];
        $tool = nt_tool_name(nt_package_name(nt_record(basename($file))) ?? '');
        $normalize = function($path) {
            if (!preg_match('#^[A-Za-z0-9_+.,/ @%()-]+$#D', $path)) throw new RuntimeException('Unsafe archive path');
            $parts = [];
            foreach (explode('/', $path) as $part) {
                if ($part === '' || $part === '.') continue;
                if ($part === '..') {
                    if (!$parts) throw new RuntimeException('Unsafe archive link');
                    array_pop($parts);
                } else $parts[] = $part;
            }
            return implode('/', $parts);
        };
        foreach ($this->command(['/bin/tar','--numeric-owner','--full-time','--quoting-style=literal','-tvf',$file], true) as $line) {
            if (!preg_match('/^([-dlh])([rwxstST-]{9})\s+(\d+)\/(\d+)\s+\d+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(.+)$/D', $line, $match)) {
                throw new RuntimeException('Unsupported or unsafe archive metadata');
            }
            $type = $match[1]; $permissions = $match[2]; $uid = (int)$match[3]; $gid = (int)$match[4]; $raw = $match[5]; $target = null;
            if ($type === 'l' || $type === 'h') {
                $parts = explode($type === 'l' ? ' -> ' : ' link to ', $raw);
                if (count($parts) !== 2) throw new RuntimeException('Unsafe archive link');
                [$raw,$target] = $parts;
            }
            if ($raw[0] === '/' || preg_match('#(?:^|/)\.\.(?:/|$)#', $raw)) throw new RuntimeException('Unsafe archive path');
            $entry = $normalize($raw);
            if (!nt_payload_allowed($tool, $entry, $type, $permissions, $uid, $gid)) throw new RuntimeException('Unsafe package destination or privilege: '.$entry);
            if ($entry === '' && $type === 'd') continue;
            if ($entry === '' || isset($records[$entry])) throw new RuntimeException('Duplicate archive path');
            if ($target !== null) {
                $target = $normalize(($type === 'l' && $target[0] !== '/' ? dirname($entry).'/' : '').$target);
                if (strpos($target, 'opt/nerdtools/') !== 0) throw new RuntimeException('Unsafe archive link');
                for ($check = $this->root.'/'.$target; $check !== $this->root && $check !== '/' && $check !== '.'; $check = dirname($check)) {
                    if (!is_link($check)) continue;
                    $resolved = realpath($check); $private = $this->root.'/opt/nerdtools/';
                    if ($resolved === false || strpos($resolved.'/', $private) !== 0) throw new RuntimeException('Unsafe archive link');
                }
            }
            $records[$entry] = ['type'=>$type, 'target'=>$target, 'raw'=>$raw];
            if ($entry === 'etc/udev/rules.d/97-hid2hci.rules') {
                $rule = $this->command(['/bin/tar','-xOf',$file,$raw], 'raw');
                if (!hash_equals(NT_BLUEZ_HID2HCI_SHA256, hash('sha256', $rule))) throw new RuntimeException('Unrecognized BlueZ hid2hci rule');
            }
            if ($entry === 'etc/pam.d/screen.new') {
                $policy = $this->command(['/bin/tar','-xOf',$file,$raw], 'raw');
                if (!hash_equals(NT_SCREEN_PAM_SHA256, hash('sha256', $policy))) throw new RuntimeException('Unrecognized Screen PAM policy');
            }
            if ($type === '-' && substr($entry, -4) === '.new') {
                if (!preg_match('#^(?:opt/nerdtools/|etc/)[A-Za-z0-9_+./-]+\.new$#D', $entry)) throw new RuntimeException('Unsafe config destination');
                $configs[] = $entry;
            }
        }
        sort($configs, SORT_STRING);
        if ($configs && !isset($records['install/doinst.sh'])) throw new RuntimeException('Missing audited config hook');
        $header = 'config() {'."\n".'  NEW="$1"'."\n".'  OLD="${NEW%.new}"'."\n"
            .'  if [ ! -e "$OLD" ] && [ ! -L "$OLD" ]; then mv "$NEW" "$OLD"; elif cmp -s "$OLD" "$NEW"; then rm "$NEW"; fi'."\n}\n";
        $expectedHook = $header;
        foreach ($configs as $config) $expectedHook .= 'config '.$config."\n";
        foreach ($records as $entry => $metadata) {
            $type = $metadata['type'];
            if ($entry === 'install' && $type === 'd') continue;
            if (strpos($entry, 'install/') === 0) {
                if ($entry === 'install/slack-desc' && $type === '-') continue;
                if ($entry === 'install/doinst.sh' && $type === '-' && $configs) {
                    $actual = $this->command(['/bin/tar','-xOf',$file,$metadata['raw']], 'raw');
                    if ($actual === $expectedHook) continue;
                }
                throw new RuntimeException('Unaudited install hook');
            }
            if ($type === 'h' && (($records[$metadata['target']]['type'] ?? '') !== '-')) throw new RuntimeException('Unsafe archive link');
            $destinations = [$entry];
            if ($type === '-' && substr($entry, -4) === '.new') $destinations[] = substr($entry, 0, -4);
            foreach ($destinations as $destination) {
                if (isset($claims['files'][$destination]) || ($type !== 'd' && isset($claims['directories'][$destination]))) {
                    throw new RuntimeException('Planned package collision: '.$destination);
                }
                $parent = dirname($destination);
                while ($parent !== '.' && $parent !== '/') {
                    if (isset($claims['files'][$parent])) throw new RuntimeException('Planned package collision: '.$destination);
                    $claims['directories'][$parent] = true; $parent = dirname($parent);
                }
                if ($type === 'd') $claims['directories'][$destination] = true;
                else $claims['files'][$destination] = $file;
            }
            if ($type === 'd') continue;
            $path = $this->root.'/'.$entry;
            if (is_dir($path) && !is_link($path)) throw new RuntimeException('Refusing to replace a directory: '.$entry);
            if ((is_link($path) || file_exists($path)) && !isset($oldFiles[$entry]) && !isset($oldFiles['./'.$entry])) {
                throw new RuntimeException('Refusing to overwrite a non-NerdTools file: '.$entry);
            }
            $parent = dirname($path);
            while ($parent !== $this->root && $parent !== '/' && $parent !== '.') {
                if (is_link($parent)) throw new RuntimeException('Refusing linked archive destination: '.$entry);
                $parent = dirname($parent);
            }
        }
    }
    private function download($package) {
        $file = $this->path.'/packages/'.$package['file'];
        if (nt_verified($file, $package)) return $file;
        if (is_link($file)) throw new RuntimeException('Refusing linked package cache');
        nt_log('Downloading '.$package['tool'].' ('.format_size($package['size']).')...');
        $tmp = tempnam($this->path.'/packages', '.download-');
        if ($tmp === false) throw new RuntimeException('Cannot create download file');
        try {
            $this->fetch($package['url'], $tmp);
            clearstatcache(true, $tmp);
            if (!nt_verified($tmp, $package)) throw new RuntimeException('SHA256 or size verification failed: '.$package['name']);
            if (!rename($tmp, $file)) throw new RuntimeException('Cannot save downloaded package');
        } finally { if (file_exists($tmp)) unlink($tmp); }
        return $file;
    }
    public function legacyOwned() {
        $owned = $this->owned(); $config = $this->config();
        $extra = $this->root.'/boot/extra'; $installed = $this->installed();
        if (is_link($extra)) throw new RuntimeException('Refusing linked legacy package directory');
        foreach (glob($extra.'/*_nerdtools.txz') ?: [] as $legacy) {
            $file = basename($legacy); $record = nt_record($file); $name = nt_package_name($record);
            if (!$name || is_link($legacy) || !is_file($legacy) || !in_array($record, $installed, true)) continue;
            if (!preg_match('/^'.preg_quote($name, '/').'-([^-]+)-(?:x86_64|noarch)-[^-]+$/D', $record, $match)) continue;
            $key = $name.'-'.str_replace('.', '__', $match[1]);
            if (!array_key_exists($key, $config) && ($owned[$name]['record'] ?? null) !== $record) continue;
            if (isset($owned[$name]) && $owned[$name]['record'] !== $record) continue;
            $owned[$name] = ['file'=>$file, 'record'=>$record, 'legacy'=>true,
                'dependencies'=>$owned[$name]['dependencies'] ?? []];
        }
        // Adoption never moves the boot archive; only successful explicit replacement removes it.
        return $owned;
    }
    private function deleteArchive($entry) {
        $file = !empty($entry['legacy']) ? $this->root.'/boot/extra/'.$entry['file'] : $this->path.'/packages/'.$entry['file'];
        if (is_link($file) || is_link(dirname($file))) throw new RuntimeException('Refusing linked owned archive');
        if (is_file($file) && !unlink($file)) throw new RuntimeException('Cannot delete owned package archive');
    }
    private function assertRemovalSafe($names) {
        if (!$names) return;
        $sources = [];
        foreach (glob($this->root.'/boot/config/plugins/*.plg') ?: [] as $plugin) {
            if (strcasecmp(basename($plugin), 'NerdTools.plg') === 0 || is_link($plugin)) continue;
            $contents = file_get_contents($plugin); $entities = [];
            if (strlen($contents) > 1048576) throw new RuntimeException('Cannot safely inspect another plugin declaration');
            preg_match_all('/<!ENTITY\s+([A-Za-z_:][A-Za-z0-9_.:-]*)\s+([\'"])(.*?)\2\s*>/s', $contents, $matches, PREG_SET_ORDER);
            foreach ($matches as $match) $entities[$match[1]] = $match[3];
            for ($depth = 0; $depth < 32; $depth++) {
                $expandedSize = strlen($contents);
                $expanded = preg_replace_callback('/&([A-Za-z_:][A-Za-z0-9_.:-]*);/', function($match) use ($entities, &$expandedSize) {
                    $replacement = $entities[$match[1]] ?? $match[0];
                    $expandedSize += strlen($replacement) - strlen($match[0]);
                    if ($expandedSize > 1048576) throw new RuntimeException('Cannot safely inspect another plugin declaration');
                    return $replacement;
                }, $contents);
                if (strlen($expanded) > 1048576) throw new RuntimeException('Cannot safely inspect another plugin declaration');
                if ($expanded === $contents) break;
                $contents = $expanded;
            }
            $sources[] = $contents;
            $runtime = $this->root.'/usr/local/emhttp/plugins/'.pathinfo($plugin, PATHINFO_FILENAME);
            if (!is_dir($runtime) || is_link($runtime)) continue;
            $files = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($runtime, FilesystemIterator::SKIP_DOTS));
            foreach ($files as $file) {
                if (!$file->isFile() || $file->isLink() || $file->getSize() > 1048576) continue;
                $text = file_get_contents($file->getPathname());
                if (strpos($text, "\0") === false) $sources[] = $text;
            }
        }
        $owned = $this->owned();
        foreach ($names as $name) {
            // Only unambiguous references count. A bare command word such as "bc" also appears in
            // unrelated text (editor syntax files, prose), which made removal impossible.
            $terms = [$name];
            foreach (($owned[$name]['commands'] ?? [nt_tool_name($name)]) as $command) {
                $terms[] = 'nerdtools-'.$command;
                $terms[] = 'opt/nerdtools/bin/'.$command; $terms[] = 'opt/nerdtools/sbin/'.$command;
            }
            foreach ($sources as $source) foreach (array_unique($terms) as $term) {
                if (preg_match('~(?<![A-Za-z0-9_+.-])'.preg_quote($term, '~').'(?=$|[^A-Za-z0-9_+.-]|-[0-9])~', $source)) {
                    throw new RuntimeException('Package is referenced by another plugin: '.$name);
                }
            }
        }
    }
    public function apply($download, $uninstall, $removeAll = false) {
        $catalog = $removeAll ? [] : $this->catalog();
        $packages = $catalog['packages'] ?? [];
        if (!$removeAll && !$catalog) throw new RuntimeException('No valid catalog cached; check for updates first');
        $config = $this->config();
        $selected = [];
        foreach ($packages as $name => $package) if (nt_preference($config, $name) === 'yes') $selected[] = $name;
        $required = $removeAll ? [] : nt_resolve($selected, $packages);
        $owned = $this->legacyOwned(); $migrations = [];
        foreach ($required as $name) {
            $tool = $packages[$name]['tool'];
            if (!isset($owned[$name]) && $tool !== $name && isset($owned[$tool])) $migrations[$name] = $tool;
        }
        // upgradepkg old%new matches every installed record with old's package basename.
        foreach ($migrations as $oldName) {
            $oldRecord = $owned[$oldName]['record']; $oldBase = nt_package_name($oldRecord);
            foreach ($this->installed() as $installed) {
                if ($installed !== $oldRecord && nt_package_name($installed) === $oldBase) {
                    throw new RuntimeException('Refusing legacy migration with another installed record sharing basename: '.$oldBase);
                }
            }
        }
        $this->assertRemovalSafe(array_values($migrations));
        // An unavailable selected package must never silently lose its dependencies.
        if (!$removeAll) foreach ($owned as $name => $entry) {
            if (!isset($packages[$name]) && !in_array($name, $migrations, true) && nt_preference($config, $name) === 'yes') throw new RuntimeException('Selected package is absent from catalog: '.$name);
        }
        if ($uninstall || $removeAll) $this->assertRemovalSafe(array_diff(array_keys($owned), $required, array_values($migrations)));
        $downloads = [];
        foreach ($required as $name) {
            $package = $packages[$name];
            $record = nt_record($package['file']);
            foreach ($this->installed() as $installed) {
                if (nt_package_name($installed) === $name && $installed !== ($owned[$name]['record'] ?? null)) {
                    throw new RuntimeException('Refusing to replace a system or externally managed package: '.$name);
                }
            }
            $file = $this->path.'/packages/'.$package['file'];
            if ($download) $file = $this->download($package);
            if (!nt_verified($file, $package)) throw new RuntimeException('Verified package is not available: '.$name);
            $downloads[$name] = $file;
        }
        // Download and verify the complete dependency closure before changing installed packages.
        $claims = ['files'=>[], 'directories'=>[]];
        foreach ($required as $name) {
            $entry = $owned[$name] ?? (isset($migrations[$name]) ? $owned[$migrations[$name]] : null);
            $oldRecord = $entry && in_array($entry['record'], $this->installed(), true) ? $entry['record'] : null;
            $this->safeArchive($downloads[$name], $oldRecord, $claims);
        }
        foreach ($required as $name) {
            $package = $packages[$name]; $record = nt_record($package['file']);
            $old = $owned[$name] ?? (isset($migrations[$name]) ? $owned[$migrations[$name]] : null);
            if ($old && $old['record'] === $record && ($old['sha256'] ?? '') === $package['sha256'] && in_array($record, $this->installed(), true)) continue;
            $oldInstalled = $old && in_array($old['record'], $this->installed(), true);
            nt_log('Installing '.$name);
            $argument = $oldInstalled ? $old['record'].'%'.$downloads[$name] : $downloads[$name];
            $this->command(['/sbin/upgradepkg','--install-new','--reinstall',$argument]);
            if (!in_array($record, $this->installed(), true)) throw new RuntimeException('Package installation did not create expected record: '.$name);
            $owned[$name] = ['file'=>$package['file'], 'record'=>$record, 'dependencies'=>$package['dependencies'],
                'sha256'=>$package['sha256'], 'size'=>$package['size'], 'tool'=>$package['tool'], 'commands'=>$package['commands']];
            if (isset($migrations[$name])) unset($owned[$migrations[$name]]);
            $this->saveOwned($owned);
            if ($old && $old['file'] !== $package['file']) $this->deleteArchive($old);
        }
        if (!$uninstall && !$removeAll) { $this->syncAliases(); return; }
        // Reverse dependency order; retain packages still needed by selected packages.
        $remove = array_diff(array_keys($owned), $required);
        $order = []; $visiting = [];
        $visit = function($name) use (&$visit, &$order, &$visiting, $owned, $remove) {
            if (isset($visiting[$name])) return;
            $visiting[$name] = true;
            foreach (($owned[$name]['dependencies'] ?? []) as $dependency) if (in_array($dependency, $remove, true)) $visit($dependency);
            $order[] = $name;
        };
        foreach ($remove as $name) $visit($name);
        foreach (array_reverse($order) as $name) {
            $entry = $owned[$name];
            if (in_array($entry['record'], $this->installed(), true)) {
                nt_log('Uninstalling '.$name);
                $this->command(['/sbin/removepkg', $entry['record']]);
                if (in_array($entry['record'], $this->installed(), true)) throw new RuntimeException('Package removal failed: '.$name);
            }
            if (!$removeAll || !empty($entry['legacy'])) $this->deleteArchive($entry);
            unset($owned[$name]); $this->saveOwned($owned);
        }
        $this->syncAliases();
    }
    public function restore() {
        $owned = $this->owned(); $config = $this->config(); $selected = [];
        foreach ($owned as $name => $entry) {
            if (empty($entry['legacy']) && nt_preference($config, $name) === 'yes') $selected[] = $name;
        }
        $required = nt_resolve($selected, $owned);
        $missing = []; $claims = ['files'=>[], 'directories'=>[]];
        foreach ($required as $name) {
            $entry = $owned[$name];
            if (in_array($entry['record'], $this->installed(), true)) continue;
            foreach ($this->installed() as $installed) {
                if (nt_package_name($installed) === $name) throw new RuntimeException('Refusing to replace a system package during restore: '.$name);
            }
            $file = $this->path.'/packages/'.$entry['file'];
            if (empty($entry['size']) || empty($entry['sha256']) || !nt_verified($file, $entry)) {
                throw new RuntimeException('Previously verified cache unavailable for '.$name.'; use Apply for an explicit download');
            }
            $this->safeArchive($file, null, $claims);
            $missing[$name] = $file;
        }
        foreach ($missing as $name => $file) {
            nt_log('Restoring cached '.$name);
            $this->command(['/sbin/upgradepkg','--install-new',$file]);
            if (!in_array($owned[$name]['record'], $this->installed(), true)) throw new RuntimeException('Package restore failed: '.$name);
        }
        $this->syncAliases();
    }
    private function hostCommandExists($command, $alias) {
        $directories = array_unique(array_merge(explode(':', getenv('PATH') ?: ''),
            ['/usr/local/sbin','/usr/local/bin','/usr/sbin','/usr/bin','/sbin','/bin']));
        foreach ($directories as $directory) {
            if ($directory === '' || $directory[0] !== '/' || strpos($directory.'/', '/opt/nerdtools/') === 0) continue;
            $candidate = $this->root.rtrim($directory, '/').'/'.$command;
            if ($candidate !== $alias && (file_exists($candidate) || is_link($candidate))) return true;
        }
        return false;
    }
    public function syncAliases() {
        $directory = $this->root.'/usr/local/bin';
        for ($parent = $directory; $parent !== $this->root && $parent !== '/' && $parent !== '.'; $parent = dirname($parent)) {
            if (is_link($parent)) throw new RuntimeException('Refusing linked command alias directory');
        }
        if (!is_dir($directory) && !mkdir($directory, 0755, true)) throw new RuntimeException('Cannot create command alias directory');
        $manifest = $this->path.'/command-aliases.json';
        $aliases = nt_json($manifest); $desired = []; $installed = $this->installed();
        foreach ($this->owned() as $name => $entry) {
            if (!in_array($entry['record'], $installed, true)) continue;
            $files = $this->packageFiles($entry['record']);
            foreach (($entry['commands'] ?? []) as $command) {
                if (!nt_name($command) || strpos($command, 'nerdtools-') === 0 || isset($desired[$command])) throw new RuntimeException('Invalid owned command metadata');
                $qualified = 'nerdtools-'.$command; $relative = 'usr/local/bin/'.$qualified;
                if ((!isset($files[$relative]) && !isset($files['./'.$relative])) || !is_file($directory.'/'.$qualified) || is_link($directory.'/'.$qualified)) {
                    throw new RuntimeException('Owned qualified command is unavailable: '.$qualified);
                }
                $desired[$command] = ['target'=>$qualified, 'package'=>$name];
            }
        }
        foreach ($aliases as $command => $entry) {
            if (!nt_name($command) || !is_array($entry) || ($entry['target'] ?? null) !== 'nerdtools-'.$command || !nt_name($entry['package'] ?? null)) {
                throw new RuntimeException('Invalid command alias ownership state');
            }
            $file = $directory.'/'.$command;
            $stat = @lstat($file);
            $unchanged = is_link($file) && readlink($file) === $entry['target'] && $stat
                && ($entry['device'] ?? null) === $stat['dev'] && ($entry['inode'] ?? null) === $stat['ino'];
            if (!isset($desired[$command]) || $this->hostCommandExists($command, $file) || !$unchanged) {
                if ($unchanged && !unlink($file)) throw new RuntimeException('Cannot remove owned command alias');
                unset($aliases[$command]);
            }
        }
        foreach ($desired as $command => $entry) {
            $file = $directory.'/'.$command;
            if (isset($aliases[$command]) || file_exists($file) || is_link($file) || $this->hostCommandExists($command, $file)) continue;
            if (!symlink($entry['target'], $file)) throw new RuntimeException('Cannot create command alias: '.$command);
            $stat = lstat($file);
            $aliases[$command] = $entry + ['device'=>$stat['dev'],'inode'=>$stat['ino']];
            nt_atomic_write($manifest, json_encode($aliases, JSON_THROW_ON_ERROR | JSON_FORCE_OBJECT));
        }
        nt_atomic_write($manifest, json_encode($aliases, JSON_THROW_ON_ERROR | JSON_FORCE_OBJECT));
    }
    public function schedule($remove = false) {
        $file = $this->path.'/NerdTools.cron';
        $contents = "# NerdTools managed schedule v1\n17 * * * * /usr/local/emhttp/plugins/NerdTools/scripts/packagemanager --check --quiet\n";
        if (is_link($file) || (file_exists($file) && (!is_file($file) || file_get_contents($file) !== $contents))) {
            throw new RuntimeException('Refusing to replace or delete an unowned schedule');
        }
        if ($remove || $this->interval() === 0) {
            if (is_file($file) && !unlink($file)) throw new RuntimeException('Cannot remove NerdTools schedule');
        } elseif (!is_file($file)) nt_atomic_write($file, $contents);
        $this->command(['/usr/local/sbin/update_cron']);
    }
}
