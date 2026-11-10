<?php
require __DIR__.'/../source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php';
function verify_alias($value,$message) { if (!$value) throw new RuntimeException($message); }
function refuse_alias($fn,$message) {
    try { $fn(); } catch (RuntimeException $error) {
        verify_alias(strpos($error->getMessage(),$message) !== false,$error->getMessage()); return;
    }
    throw new RuntimeException('Expected refusal: '.$message);
}
$root = sys_get_temp_dir().'/nerdtools-aliases-'.bin2hex(random_bytes(6));
foreach (['etc','var/log/packages','usr/bin','usr/local/bin','boot/extra','boot/config/plugins','opt/nerdtools/bin','custom/bin'] as $path) mkdir($root.'/'.$path,0755,true);
file_put_contents($root.'/etc/unraid-version','version="7.3.4"');
$payload = 'verified native archive'; $corrupt = false; $calls = [];
$package = function($tool,$dependencies = []) use ($payload) {
    return ['name'=>'nerdtools-'.$tool,'tool'=>$tool,'version'=>'2.0','file'=>'nerdtools-'.$tool.'-2.0-x86_64-1.txz',
        'sha256'=>hash('sha256',$payload),'size'=>strlen($payload),'dependencies'=>$dependencies,'commands'=>[$tool]];
};
$catalog = ['family'=>'7.3','min_unraid'=>'7.3.4','packages'=>[$package('dep'),$package('tool',['nerdtools-dep'])]];
$transport = function($url,$file) use ($catalog,$payload,&$corrupt) {
    if ($file) { file_put_contents($file,$corrupt ? 'bad bytes' : $payload); return ['body'=>'']; }
    if (strpos($url,'api.github.com') !== false) {
        $assets = [['name'=>'catalog-7.3.json','size'=>1,'browser_download_url'=>'https://github.com/'.NT_REPOSITORY.'/releases/download/native/catalog-7.3.json']];
        foreach ($catalog['packages'] as $item) $assets[] = ['name'=>$item['file'],'size'=>$item['size'],
            'browser_download_url'=>'https://github.com/'.NT_REPOSITORY.'/releases/download/native/'.$item['file']];
        return ['body'=>json_encode([['tag_name'=>'native','draft'=>false,'prerelease'=>false,'assets'=>$assets]])];
    }
    return ['body'=>json_encode($catalog)];
};
$removeRecord = function($record) use ($root) {
    $log = $root.'/var/log/packages/'.$record;
    $text = file_get_contents($log); $files = explode("\n",substr($text,strpos($text,"FILE LIST:\n")+11));
    foreach ($files as $file) if ($file !== '' && (is_file($root.'/'.$file) || is_link($root.'/'.$file))) unlink($root.'/'.$file);
    unlink($log);
};
$runner = function($args,$capture) use ($root,&$calls,$removeRecord) {
    $calls[] = $args;
    if ($args[0] === '/usr/local/sbin/update_cron') return [];
    if ($args[0] === '/bin/tar') {
        $name = nt_package_name(nt_record(basename(end($args)))); $tool = nt_tool_name($name);
        return ['-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 usr/local/bin/nerdtools-'.$tool,
            '-rwxr-xr-x 0/0 10 2026-01-01 00:00:00 opt/nerdtools/bin/'.$tool];
    }
    if ($args[0] === '/sbin/upgradepkg') {
        $argument = end($args); $parts = explode('%',$argument,2);
        if (count($parts) === 2) {
            $oldBase = nt_package_name($parts[0]);
            foreach (array_diff(scandir($root.'/var/log/packages'),['.','..']) as $installed) {
                if (nt_package_name($installed) === $oldBase) $removeRecord($installed);
            }
            $argument = $parts[1];
        }
        $record = nt_record(basename($argument)); $tool = nt_tool_name(nt_package_name($record));
        $files = ['usr/local/bin/nerdtools-'.$tool,'opt/nerdtools/bin/'.$tool];
        foreach ($files as $file) { file_put_contents($root.'/'.$file,'qualified command'); chmod($root.'/'.$file,0755); }
        file_put_contents($root.'/var/log/packages/'.$record,"FILE LIST:\n".implode("\n",$files)."\n");
        return [];
    }
    if ($args[0] === '/sbin/removepkg') { $removeRecord($args[1]); return []; }
    throw new RuntimeException('Unexpected package command');
};
$manager = new NerdTools($root,$transport,$runner);
file_put_contents($manager->path.'/NerdTools.cfg',"tool-1__0=\"yes\"\nprivate_setting=\"keep\"\n");
file_put_contents($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz','original legacy bytes');
file_put_contents($root.'/var/log/packages/tool-1.0-x86_64-1_nerdtools',"FILE LIST:\nusr/local/bin/tool\n");
file_put_contents($root.'/usr/local/bin/tool','legacy command');
file_put_contents($root.'/var/log/packages/tool-9.0-x86_64-1',"FILE LIST:\nusr/bin/tool\n");
file_put_contents($root.'/usr/bin/tool','host command');
$manager->refresh(true);
verify_alias(nt_preference($manager->config(),'nerdtools-tool') === 'yes','Original selected preference follows tool identity');
$manager->save(['nerdtools-tool'=>'yes','nerdtools-dep'=>'no'],24);
verify_alias(($manager->config()['nerdtools-tool'] ?? null) === 'yes' && !isset($manager->config()['tool-1__0']),'Settings migrate once to prefixed identity');
verify_alias(($manager->config()['private_setting'] ?? null) === 'keep','Unrelated settings survive identity migration');
$manager->restore();
verify_alias(is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz') && file_get_contents($root.'/usr/local/bin/tool') === 'legacy command','Saving/boot do not remove old working command or next-boot archive');
$upgradeCount = count(array_filter($calls,function($args) { return $args[0] === '/sbin/upgradepkg'; }));
refuse_alias(function() use ($manager) { $manager->apply(true,true); },'another installed record sharing basename');
verify_alias(in_array('tool-9.0-x86_64-1',$manager->installed(),true) && file_get_contents($root.'/usr/bin/tool') === 'host command','Migration refuses pkgtools basename collision without touching system record or files');
verify_alias(count(array_filter($calls,function($args) { return $args[0] === '/sbin/upgradepkg'; })) === $upgradeCount,'Basename conflict stops before native pkgtools invocation');
// Simulate an administrator resolving the record collision; retain the host command file.
unlink($root.'/var/log/packages/tool-9.0-x86_64-1');
file_put_contents($root.'/boot/config/plugins/Other.plg','<!DOCTYPE PLUGIN [<!ENTITY first "to"><!ENTITY last "ol">]><PLUGIN><FILE>&first;&last;-1.0.txz</FILE></PLUGIN>');
refuse_alias(function() use ($manager) { $manager->apply(true,false); },'referenced by another plugin');
verify_alias(in_array('tool-1.0-x86_64-1_nerdtools',$manager->installed(),true) && is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz'),'Positive dependency veto applies before migration even without uninstall flag');
unlink($root.'/boot/config/plugins/Other.plg');
$corrupt = true;
refuse_alias(function() use ($manager) { $manager->apply(true,true); },'verification failed');
verify_alias(is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz'),'Failed migration preserves legacy boot artifact');
$corrupt = false;
file_put_contents($root.'/usr/local/bin/nerdtools-tool','foreign qualified command');
refuse_alias(function() use ($manager) { $manager->apply(true,true); },'non-NerdTools file');
verify_alias(file_get_contents($root.'/usr/local/bin/nerdtools-tool') === 'foreign qualified command','Qualified namespace collision does not overwrite a foreign command');
unlink($root.'/usr/local/bin/nerdtools-tool');
$manager->apply(true,true);
verify_alias(isset($manager->owned()['nerdtools-tool']) && !isset($manager->owned()['tool']),'Exact legacy ownership migrates to new package identity');
verify_alias(file_get_contents($root.'/usr/bin/tool') === 'host command','Qualified runtime migration cannot replace an existing host command');
verify_alias(!is_file($root.'/boot/extra/tool-1.0-x86_64-1_nerdtools.txz'),'Legacy archive retired only after successful verified migration');
verify_alias(is_file($root.'/usr/local/bin/nerdtools-tool') && !is_link($root.'/usr/local/bin/tool'),'Host command conflict leaves qualified command and no shadowing alias');
verify_alias(is_link($root.'/usr/local/bin/dep') && readlink($root.'/usr/local/bin/dep') === 'nerdtools-dep','Bare command works when absent on host');
// Full service PATH, not just /usr/bin, vetoes alias creation.
unlink($root.'/usr/bin/tool');
$oldPath = getenv('PATH'); putenv('PATH=/custom/bin:/usr/local/bin:/usr/bin');
file_put_contents($root.'/custom/bin/tool','custom host command');
$manager->syncAliases();
verify_alias(!is_link($root.'/usr/local/bin/tool'),'Custom host PATH command is not shadowed');
unlink($root.'/custom/bin/tool'); $manager->syncAliases();
verify_alias(is_link($root.'/usr/local/bin/tool') && readlink($root.'/usr/local/bin/tool') === 'nerdtools-tool','Absent host command gets tracked bare alias');
file_put_contents($root.'/usr/bin/tool','new host command'); $manager->syncAliases();
verify_alias(!is_link($root.'/usr/local/bin/tool') && file_get_contents($root.'/usr/bin/tool') === 'new host command','New host command retires only owned shadowing alias');
unlink($root.'/usr/bin/tool'); $manager->syncAliases();
unlink($root.'/usr/local/bin/tool'); symlink('foreign-target',$root.'/usr/local/bin/tool'); $manager->syncAliases();
verify_alias(is_link($root.'/usr/local/bin/tool') && readlink($root.'/usr/local/bin/tool') === 'foreign-target','Foreign symlink replacement is preserved');
unlink($root.'/usr/local/bin/tool'); file_put_contents($root.'/usr/local/bin/tool','foreign replacement');
$manager->save(['nerdtools-tool'=>'no','nerdtools-dep'=>'no'],24);
$manager->apply(true,true);
verify_alias(file_get_contents($root.'/usr/local/bin/tool') === 'foreign replacement','Uninstall never deletes foreign alias replacement');
verify_alias(!is_link($root.'/usr/local/bin/dep') && !is_file($root.'/usr/local/bin/nerdtools-dep'),'Uninstall removes only unchanged owned aliases and qualified payload');
if ($oldPath === false) putenv('PATH'); else putenv('PATH='.$oldPath);
$iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root,FilesystemIterator::SKIP_DOTS),RecursiveIteratorIterator::CHILD_FIRST);
foreach ($iterator as $entry) { if ($entry->isDir() && !$entry->isLink()) rmdir($entry->getPathname()); else unlink($entry->getPathname()); }
rmdir($root);
echo "Prefixed migration and command alias contracts passed\n";
