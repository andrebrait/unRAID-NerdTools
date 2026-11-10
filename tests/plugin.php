<?php
// Run with php tests/plugin.php; all state is isolated below a temporary directory.
require __DIR__.'/../source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php';
function check($value, $message) { if (!$value) throw new RuntimeException($message); }
function rejects($fn) { try { $fn(); } catch (RuntimeException $e) { return; } throw new RuntimeException('Expected refusal'); }
$root = sys_get_temp_dir().'/nerdtools-test-'.bin2hex(random_bytes(6));
mkdir($root);
$pkg = ['name'=>'tool','version'=>'1.2','file'=>'tool-1.2-x86_64-1.txz','sha256'=>str_repeat('a',64),'size'=>123,'dependencies'=>[]];
$catalog = ['family'=>'7.3','min_unraid'=>'7.3.4','packages'=>[$pkg]];
$asset = ['name'=>$pkg['file'],'size'=>123,'browser_download_url'=>'https://github.com/andrebrait/unRAID-NerdTools/releases/download/test/'.$pkg['file']];
$validated = nt_validate_catalog($catalog, [$asset], '7.3.4');
check(isset($validated['packages']['tool']), 'Catalog has indexed package');
$providedCatalog = $catalog;
$providedCatalog['provided_packages'] = [['name'=>'liburing','version'=>'2.15','provider'=>'unraid','min_unraid'=>'7.3.4']];
$provided = nt_validate_catalog($providedCatalog,[$asset],'7.3.4');
check($provided['provided_packages'][0]['name'] === 'liburing' && !isset($provided['packages']['liburing']), 'Built-in capability is not an installable package');
$providedCatalog['provided_packages'][0]['file'] = 'fake.txz';
rejects(function() use ($providedCatalog,$asset) { nt_validate_catalog($providedCatalog,[$asset],'7.3.4'); });
rejects(function() use ($catalog,$asset) { nt_validate_catalog($catalog,[$asset],'7.4.0'); });
rejects(function() use ($catalog,$asset) { nt_validate_catalog($catalog,[$asset],'7.3.3'); });
rejects(function() use ($catalog) { nt_validate_catalog($catalog,[],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['file'] = '../tool.txz';
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['sha256'] = 'bogus';
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['dependencies'] = ['missing'];
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$bad = $catalog; $bad['packages'][0]['dependencies'] = ['tool'];
rejects(function() use ($bad,$asset) { nt_validate_catalog($bad,[$asset],'7.3.4'); });
$packages = $validated['packages'];
$packages['dep'] = array_merge($packages['tool'], ['name'=>'dep','dependencies'=>[]]);
$packages['tool']['dependencies'] = ['dep'];
check(nt_resolve(['tool'], $packages) === ['dep','tool'], 'Dependency-first order');
check(nt_preference(['tool-1__1'=>'yes'], 'tool') === 'yes', 'Legacy version preference retained');
check(nt_preference(['tool-1__1'=>'yes','tool'=>'no'], 'tool') === 'no', 'Stable preference wins');
check(nt_preference(['tool-extra-1__1'=>'yes'], 'tool') === 'no', 'No package prefix collision');
check(nt_next_check(1000, 24, false, 0) === 87400, 'Daily default interval');
check(nt_next_check(1000, 24, true, 0) === 4600, 'Hourly failure retry');
check(nt_next_check(1000, 24, true, 9000) === 9000, 'Rate limit reset wins');
file_put_contents($root.'/cache.json', '{"old":true}');
nt_atomic_write($root.'/cache.json', '{"new":true}');
check(json_decode(file_get_contents($root.'/cache.json'),true)['new'], 'Atomic cache replacement');
file_put_contents($root.'/payload', 'verified bytes');
check(nt_verified($root.'/payload', ['size'=>14,'sha256'=>hash('sha256','verified bytes')]), 'Valid digest accepted');
check(!nt_verified($root.'/payload', ['size'=>14,'sha256'=>str_repeat('0',64)]), 'Bad digest rejected');
foreach (glob($root.'/*') as $file) unlink($file);
rmdir($root);
echo "Plugin contracts passed\n";
