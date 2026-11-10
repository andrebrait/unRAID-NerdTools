<?php
require __DIR__.'/../source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php';
function expect($condition, $message) { if (!$condition) throw new RuntimeException($message); }
function refused($fn, $text) {
    try { $fn(); } catch (RuntimeException $error) {
        expect(strpos($error->getMessage(), $text) !== false, 'Unexpected refusal: '.$error->getMessage()); return;
    }
    throw new RuntimeException('Expected refusal: '.$text);
}
$root = sys_get_temp_dir().'/nerdtools-lifecycle-'.bin2hex(random_bytes(6));
foreach (['etc/cron.d','var/log/packages','usr/bin','opt/nerdtools/bin','boot/extra','boot/config/plugins'] as $directory) mkdir($root.'/'.$directory, 0755, true);
file_put_contents($root.'/etc/unraid-version', 'version="7.3.4"');
$bytes = 'verified archive';
$makePackage = function($name, $version, $dependencies = []) use ($bytes) {
    return ['name'=>$name,'version'=>$version,'file'=>$name.'-'.$version.'-x86_64-1.txz',
        'sha256'=>hash('sha256',$bytes),'size'=>strlen($bytes),'dependencies'=>$dependencies];
};
$catalog = ['family'=>'7.3','min_unraid'=>'7.3.4','packages'=>[$makePackage('dep','1.0'),$makePackage('tool','1.0',['dep'])],
    'external_packages'=>[['name'=>'rar','version'=>'7.23','delivery'=>'external',
        'url'=>'https://www.rarlab.com/rar/rarlinux-x64-723.tar.gz','license_url'=>'https://www.rarlab.com/license.htm',
        'license_notice'=>'RAR is proprietary trial software; a license is required after 40 days. Install the original distribution yourself.']]];
$olderCatalog = $catalog;
$requests = []; $commands = []; $failure = null; $corrupt = false;
$archiveOverrides = []; $hookOverrides = [];
$transport = function($url,$file) use (&$requests,&$catalog,&$failure,&$corrupt,$bytes,$olderCatalog) {
    $requests[] = $url;
    if ($failure) throw $failure;
    if ($file) { file_put_contents($file, $corrupt ? 'broken' : $bytes); return ['body'=>'']; }
    if (strpos($url, 'api.github.com') !== false) {
        $assetsFor = function($data,$tag) {
            $assets = [['name'=>'catalog-7.3.json','size'=>200,'browser_download_url'=>'https://github.com/'.NT_REPOSITORY.'/releases/download/'.$tag.'/catalog-7.3.json']];
            foreach ($data['packages'] as $package) $assets[] = ['name'=>$package['file'],'size'=>$package['size'],
                'browser_download_url'=>'https://github.com/'.NT_REPOSITORY.'/releases/download/'.$tag.'/'.$package['file']];
            return $assets;
        };
        $assets = $assetsFor($catalog,'packages-test');
        return ['body'=>json_encode([
            ['tag_name'=>'plugin-only','draft'=>false,'prerelease'=>false,'assets'=>[]],
            ['tag_name'=>'preview','draft'=>false,'prerelease'=>true,'assets'=>$assets],
            ['tag_name'=>'packages-test','draft'=>false,'prerelease'=>false,'assets'=>$assets],
            ['tag_name'=>'packages-old','draft'=>false,'prerelease'=>false,'assets'=>$assetsFor($olderCatalog,'packages-old')],
        ])];
    }
    return ['body'=>json_encode(strpos($url, '/packages-old/') !== false ? $olderCatalog : $catalog)];
};
$runner = function($args,$capture) use (&$commands,$root,&$archiveOverrides,&$hookOverrides) {
    $commands[] = $args;
    if ($args[0] === '/usr/local/sbin/update_cron') return [];
    if ($args[0] === '/bin/tar') {
        $file = $args[1] === '-xOf' ? $args[2] : end($args);
        $name = nt_package_name(nt_record(basename($file)));
        if ($args[1] === '-xOf') return $hookOverrides[$name] ?? [];
        return $archiveOverrides[$name] ?? [
            'drwxr-xr-x 0/0 0 2026-01-01 00:00:00 opt/',
            'drwxr-xr-x 0/0 0 2026-01-01 00:00:00 opt/nerdtools/bin/',
            '-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/'.$name,
        ];
    }
    if ($args[0] === '/sbin/upgradepkg') {
        $argument = end($args); $parts = explode('%',$argument,2);
        if (count($parts) === 2) {
            $oldBase = nt_package_name($parts[0]);
            foreach (array_diff(scandir($root.'/var/log/packages'),['.','..']) as $installed) {
                if (nt_package_name($installed) !== $oldBase) continue;
                $log = $root.'/var/log/packages/'.$installed; $text = file_get_contents($log);
                foreach (explode("\n",substr($text,strpos($text,"FILE LIST:\n")+11)) as $oldFile) {
                    if ($oldFile !== '' && (is_file($root.'/'.$oldFile) || is_link($root.'/'.$oldFile))) unlink($root.'/'.$oldFile);
                }
                unlink($log);
            }
            $argument = $parts[1];
        }
        $record = nt_record(basename($argument)); $name = nt_package_name($record);
        file_put_contents($root.'/var/log/packages/'.$record, "PACKAGE NAME: $record\nFILE LIST:\nopt/nerdtools/bin/$name\n");
        file_put_contents($root.'/opt/nerdtools/bin/'.$name,'installed');
        return [];
    }
    if ($args[0] === '/sbin/removepkg') {
        unlink($root.'/var/log/packages/'.$args[1]);
        $file = $root.'/opt/nerdtools/bin/'.nt_package_name($args[1]);
        if (is_file($file)) unlink($file);
        return [];
    }
    throw new RuntimeException('Unexpected command');
};
$manager = new NerdTools($root,$transport,$runner);
$manager->refresh(true);
expect($manager->catalog()['release'] === 'packages-test', 'Skip plugin-only and prerelease releases');
$manager->save(['tool'=>'yes','dep'=>'no'],24);
$archiveOverrides['tool'] = ['-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 install/doinst.sh'];
$hookOverrides['tool'] = ['#!/bin/sh','rm -rf /'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unaudited install hook');
$archiveOverrides['tool'] = ['-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/dep'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Planned package collision');
$archiveOverrides['tool'] = ['lrwxrwxrwx 0/0 0 2026-01-01 00:00:00 opt/nerdtools/link -> ../../etc/shadow'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unsafe archive link');
$archiveOverrides['tool'] = ['hrwxr-xr-x 0/0 0 2026-01-01 00:00:00 opt/nerdtools/link link to etc/shadow'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unsafe archive link');
$archiveOverrides['tool'] = ['-rw-r--r-- 0/0 10 2026-01-01 00:00:00 boot/config/new-unowned-file'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unsafe package destination or privilege');
$archiveOverrides['tool'] = ['-rwsr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/tool'];
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unsafe package destination or privilege');
$archiveOverrides = []; $hookOverrides = [];
$archiveOverrides['tool'] = [
    '-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/tool',
    '-rw-r--r-- 0/0 10 2026-01-01 00:00:00 opt/nerdtools/share/alsa/Librem 5,codec.conf',
    '-rw-r--r-- 0/0 10 2026-01-01 00:00:00 opt/nerdtools/etc/tool.conf.new',
    '-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 install/doinst.sh',
];
$hookOverrides['tool'] = ['config() {','  NEW="$1"','  OLD="${NEW%.new}"',
    '  if [ ! -e "$OLD" ] && [ ! -L "$OLD" ]; then mv "$NEW" "$OLD"; elif cmp -s "$OLD" "$NEW"; then rm "$NEW"; fi',
    '}', 'config opt/nerdtools/etc/tool.conf.new'];
$hookOverrides['tool'][1] .= "\r";
refused(function() use ($manager) { $manager->apply(true,true); }, 'Unaudited install hook');
$hookOverrides['tool'][1] = '  NEW="$1"';
$manager->apply(true,true);
$archiveOverrides = []; $hookOverrides = [];
expect(array_keys($manager->owned()) === ['dep','tool'], 'Dependency-first installation and ownership');
expect(count($manager->installed()) === 2, 'Both packages installed');
$manager->apply(true,true);
expect(count(array_filter($commands,function($args) { return $args[0] === '/sbin/upgradepkg'; })) === 2, 'No redundant installations');
// Atomic invalid-response handling retains a usable last-good catalog.
$good = file_get_contents($manager->path.'/catalog-cache.json');
$catalog['packages'][0]['sha256'] = 'bad';
refused(function() use ($manager) { $manager->refresh(true); }, 'Invalid package');
expect(file_get_contents($manager->path.'/catalog-cache.json') === $good, 'Invalid catalog preserves cache');
expect($manager->state()['next_check'] >= time()+3590, 'Failure schedules hourly retry');
$deadline = $manager->state()['next_check'];
$manager->save(['tool'=>'yes','dep'=>'no'],168);
expect($manager->state()['next_check'] === $deadline, 'Saving a longer interval cannot postpone hourly retry');
$catalog['packages'][0] = $makePackage('dep','1.0');
$failure = new NerdToolsHttpError('rate limited',time()+7200);
refused(function() use ($manager) { $manager->refresh(true); }, 'rate limited');
$count = count($requests);
expect($manager->refresh(true) === false && count($requests) === $count, 'Force respects server reset');
$failure = null;
nt_atomic_write($manager->path.'/check-state.json','{}');
$manager->save(['tool'=>'yes','dep'=>'no'],0);
$count = count($requests);
expect($manager->refresh() === false && count($requests) === $count, 'Disabled means no background HTTP');
$manager->refresh(true);
// Upgrade keeps dependencies, and removes only the exact old owned archive.
$catalog['packages'][1] = $makePackage('tool','2.0',['dep']);
$manager->refresh(true);
// Reboot restores the owned cached versions, not a newer catalog's versions.
foreach ($manager->installed() as $record) {
    unlink($root.'/var/log/packages/'.$record);
    unlink($root.'/opt/nerdtools/bin/'.nt_package_name($record));
}
$requestCount = count($requests);
$archiveOverrides['tool'] = ['-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/dep'];
refused(function() use ($manager) { $manager->restore(); }, 'Planned package collision');
expect(!$manager->installed(), 'Restore collision fails before installing any archive');
$archiveOverrides = [];
$manager->restore();
expect(in_array('tool-1.0-x86_64-1',$manager->installed(),true), 'Boot restores last explicitly installed version');
expect(!in_array('tool-2.0-x86_64-1',$manager->installed(),true) && count($requests) === $requestCount, 'Boot performs no package download or implicit upgrade');
file_put_contents($root.'/boot/extra/tool-other-unrelated.txz','not owned');
$manager->apply(true,true);
expect(in_array('tool-2.0-x86_64-1',$manager->installed(),true), 'New package installed');
expect(!is_file($manager->path.'/packages/tool-1.0-x86_64-1.txz'), 'Only old owned archive removed');
expect(is_file($root.'/boot/extra/tool-other-unrelated.txz'), 'Unrelated extra archive preserved');
$manager->refresh(true,'packages-old');
$manager->apply(true,true);
expect(in_array('tool-1.0-x86_64-1',$manager->installed(),true), 'Retained release can downgrade an owned package');
expect($manager->catalog()['selector'] === 'packages-old', 'Specific release is pinned');
$manager->refresh(true);
expect($manager->catalog()['release'] === 'packages-old', 'Checks preserve selected release');
$manager->refresh(true,'latest');
$manager->apply(true,true);
expect(in_array('tool-2.0-x86_64-1',$manager->installed(),true), 'Latest unpins and restores newest version');
$manager->save(['tool'=>'no','dep'=>'no'],24);
file_put_contents($root.'/boot/config/plugins/Other.plg', '<!DOCTYPE PLUGIN [<!ENTITY stem "de"><!ENTITY suffix "p"><!ENTITY pkg "&stem;&suffix;"><!ENTITY ver "1.0">]><PLUGIN><FILE>&pkg;-&ver;-x86_64-1.txz</FILE></PLUGIN>');
refused(function() use ($manager) { $manager->apply(true,true); }, 'referenced by another plugin');
expect(count($manager->installed()) === 2, 'Entity-expanded dependency veto runs before any removal');
unlink($root.'/boot/config/plugins/Other.plg');
file_put_contents($root.'/boot/config/plugins/Other.plg','<PLUGIN name="Other"/>');
mkdir($root.'/usr/local/emhttp/plugins/Other',0755,true);
file_put_contents($root.'/usr/local/emhttp/plugins/Other/run',"#!/bin/sh\n/opt/nerdtools/bin/tool --version\n");
refused(function() use ($manager) { $manager->apply(true,true); }, 'referenced by another plugin');
unlink($root.'/usr/local/emhttp/plugins/Other/run');
rmdir($root.'/usr/local/emhttp/plugins/Other');
$manager->apply(true,true);
unlink($root.'/boot/config/plugins/Other.plg');
expect(!$manager->installed() && !$manager->owned(), 'Unselected dependency closure removed');
$removals = array_values(array_filter($commands,function($args) { return $args[0] === '/sbin/removepkg'; }));
expect(nt_package_name($removals[0][1]) === 'tool' && nt_package_name($removals[1][1]) === 'dep', 'Dependants removed before dependencies');
// Hash mismatch fails before any package command and does not replace a valid download.
$manager->save(['tool'=>'yes'],24); $corrupt = true;
$count = count($commands);
refused(function() use ($manager) { $manager->apply(true,true); }, 'verification failed');
expect(count($commands) === $count && !$manager->installed(), 'Bad bytes cannot reach package manager');
$corrupt = false;
// External/system packages are never adopted or upgraded merely because names match.
file_put_contents($root.'/var/log/packages/tool-9.0-x86_64-1',"FILE LIST:\nusr/bin/tool\n");
refused(function() use ($manager) { $manager->apply(true,true); }, 'system or externally managed');
unlink($root.'/var/log/packages/tool-9.0-x86_64-1');
file_put_contents($root.'/opt/nerdtools/bin/tool','system-owned file');
$installCount = count(array_filter($commands,function($args) { return $args[0] === '/sbin/upgradepkg'; }));
refused(function() use ($manager) { $manager->apply(true,true); }, 'non-NerdTools file');
expect(file_get_contents($root.'/opt/nerdtools/bin/tool') === 'system-owned file', 'Conflicting system file is untouched');
expect(count(array_filter($commands,function($args) { return $args[0] === '/sbin/upgradepkg'; })) === $installCount, 'All archive paths preflight before any dependency install');
unlink($root.'/opt/nerdtools/bin/tool');
// Legacy preferences and owned _nerdtools archives migrate without broad deletion.
file_put_contents($manager->path.'/NerdTools.cfg', "tool-1__0=\"yes\"\nprivate_setting=\"keep\"\n");
file_put_contents($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz','legacy archive');
file_put_contents($root.'/var/log/packages/tool-1.0-x86_64-1_nerdtools',"FILE LIST:\nusr/bin/tool\n");
file_put_contents($root.'/usr/bin/tool','legacy');
file_put_contents($root.'/boot/extra/tool-3.0-x86_64-1_nerdtools.txz','different legacy version');
expect(is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz'), 'Legacy boot archive remains until explicit verified migration');
file_put_contents($manager->path.'/packages/tool-2.0-x86_64-1.txz','corrupt cache');
$corrupt = true;
refused(function() use ($manager) { $manager->apply(true,true); }, 'verification failed');
expect(is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz') && in_array('tool-1.0-x86_64-1_nerdtools',$manager->installed(),true), 'Failed explicit migration preserves legacy installation and next-boot archive');
$corrupt = false;
$manager->apply(true,true);
expect(in_array('tool-2.0-x86_64-1',$manager->installed(),true), 'Legacy ownership upgraded');
expect(!is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz'), 'Exact migrated legacy archive removed');
expect(is_file($root.'/boot/extra/tool-3.0-x86_64-1_nerdtools.txz'), 'Different legacy version is not ownership evidence');
$manager->save(['tool'=>'yes'],24);
expect(($manager->config()['private_setting'] ?? null) === 'keep' && !isset($manager->config()['tool-1__0']), 'Preserve settings and migrate only known legacy keys');
$manager->schedule();
expect(strpos(file_get_contents($manager->path.'/NerdTools.cron'),'--check --quiet') !== false, 'Native persistent plugin scheduling');
file_put_contents($manager->path.'/NerdTools.cron','unrelated schedule');
refused(function() use ($manager) { $manager->schedule(); }, 'unowned schedule');
refused(function() use ($manager) { $manager->schedule(true); }, 'unowned schedule');
expect(file_get_contents($manager->path.'/NerdTools.cron') === 'unrelated schedule', 'Existing foreign cron is not replaced or deleted');
unlink($manager->path.'/NerdTools.cron');
$manager->schedule();
$manager->apply(false,true,true); $manager->schedule(true);
expect(!is_file($manager->path.'/NerdTools.cron') && is_file($manager->path.'/NerdTools.cfg'), 'Removal preserves configuration');
expect(is_file($manager->path.'/packages/tool-2.0-x86_64-1.txz'), 'Plugin removal preserves verified cache');
expect(is_file($root.'/boot/extra/tool-other-unrelated.txz'), 'Removal never deletes unrelated extras');
// Safe test cleanup is restricted to this random temporary root.
$iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root,FilesystemIterator::SKIP_DOTS),RecursiveIteratorIterator::CHILD_FIRST);
foreach ($iterator as $entry) { if ($entry->isDir()) rmdir($entry->getPathname()); else unlink($entry->getPathname()); }
rmdir($root);
echo "Plugin lifecycle contracts passed\n";
