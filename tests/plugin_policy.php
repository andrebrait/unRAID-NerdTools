<?php
require __DIR__.'/../source/NerdTools/usr/local/emhttp/plugins/NerdTools/include/NerdToolsHelpers.php';
function policy_check($value,$message) { if (!$value) throw new RuntimeException($message); }
$allowed = [
    ['vim','opt/nerdtools/bin/vim','-','rwxr-xr-x',0,0],
    ['vim','opt/nerdtools/lib64/libfoo.so','l','rwxrwxrwx',0,0],
    ['vim','usr/local/bin/nerdtools-vim','-','rwxr-xr-x',0,0],
    ['plocate','opt/nerdtools/bin/plocate','-','rwx--s--x',0,21],
    ['mlocate','opt/nerdtools/bin/mlocate','-','rwx--s--x',0,21],
    ['plocate','opt/nerdtools/var/lib/plocate','d','rwxr-x---',0,21],
    ['mlocate','opt/nerdtools/var/lib/mlocate','d','rwxr-x---',0,21],
    ['audit','etc/rc.d/rc.auditd.new','-','rw-r--r--',0,0],
    ['screen','etc/screenrc.new','-','rw-r--r--',0,0],
    ['screen','etc/skel/.screenrc.new','-','rw-r--r--',0,0],
    ['screen','etc/pam.d/screen.new','-','rw-r--r--',0,0],
    ['bluez','etc/udev/rules.d/97-hid2hci.rules','-','rw-r--r--',0,0],
    ['bluez','usr/share/dbus-1/system.d/bluetooth.conf','-','rw-r--r--',0,0],
    ['bluez','usr/share/dbus-1/system.d/obex.conf','-','rw-r--r--',0,0],
    ['bluez','etc/bluetooth/mesh-main.conf.new','-','rw-r--r--',0,0],
    ['bluez','usr/share/dbus-1/system.d/bluetooth-mesh.conf','-','rw-r--r--',0,0],
    ['bluez','usr/share/dbus-1/services/org.bluez.obex.service','-','rw-r--r--',0,0],
    ['bluez','var/lib/bluetooth','d','rwx------',0,0],
];
foreach ($allowed as $case) policy_check(nt_payload_allowed(...$case),'Required legitimate payload rejected: '.$case[1]);
$denied = [
    ['vim','etc/shadow','-','rw-------',0,0],
    ['vim','boot/config/go','-','rwxr-xr-x',0,0],
    ['vim','root/new-file','-','rw-r--r--',0,0],
    ['vim','usr/bin/vim','-','rwxr-xr-x',0,0],
    ['vim','usr/local/bin/vim','-','rwxr-xr-x',0,0],
    ['vim','usr/local/bin/nerdtools-vim','l','rwxrwxrwx',0,0],
    ['vim','opt/nerdtools/bin/vim','-','rwsr-xr-x',0,0],
    ['vim','opt/nerdtools/bin/vim','-','rwxr-sr-x',0,0],
    ['vim','opt/nerdtools/bin/vim','-','rwxrwxrwx',0,0],
    ['vim','opt/nerdtools/bin/vim','-','rwxr-xr-x',1,0],
    ['vim','opt/nerdtools/bin/vim','-','rwxr-xr-x',0,21],
    ['plocate','opt/nerdtools/bin/plocate','-','rwxr-xr-x',0,0],
    ['mlocate','opt/nerdtools/bin/locate','-','rwx--s--x',0,21],
    ['vim','opt/nerdtools/bin/plocate','-','rwx--s--x',0,21],
    ['plocate','opt/nerdtools/var/lib/plocate','d','rwxrwx---',0,21],
    ['audit','etc/rc.d/rc.auditd.new','-','rwxr-xr-x',0,0],
    ['vim','etc/rc.d/rc.auditd.new','-','rw-r--r--',0,0],
    ['vim','etc/pam.d/screen.new','-','rw-r--r--',0,0],
    ['screen','etc/pam.d/screen','-','rw-r--r--',0,0],
    ['screen','etc/pam.d/screen.new','-','rwxr-xr-x',0,0],
    ['bluez','etc/udev/rules.d/unapproved.rules','-','rw-r--r--',0,0],
    ['bluez','usr/share/dbus-1/system.d/foreign.conf','-','rw-r--r--',0,0],
    ['bluez','var/lib/bluetooth/state','-','rw-------',0,0],
    ['bluez','var/lib/bluetooth','d','rwxr-xr-x',0,0],
];
foreach ($denied as $case) policy_check(!nt_payload_allowed(...$case),'Unsafe payload accepted: '.$case[1]);
foreach (['rar','nerdtools-rar'] as $name) {
    $package = ['name'=>$name,'version'=>'7.23','file'=>$name.'-7.23-x86_64-1.txz','sha256'=>str_repeat('a',64),'size'=>1,'dependencies'=>[]];
    if ($name === 'nerdtools-rar') $package['tool'] = 'rar';
    $asset = ['name'=>$package['file'],'size'=>1,'browser_download_url'=>'https://github.com/'.NT_REPOSITORY.'/releases/download/test/'.$package['file']];
    try {
        nt_validate_catalog(['family'=>'7.3','min_unraid'=>'7.3.4','packages'=>[$package]],[$asset],'7.3.4');
    } catch (RuntimeException $error) {
        policy_check(strpos($error->getMessage(),'RAR must remain external') !== false,$error->getMessage()); continue;
    }
    throw new RuntimeException('Downloadable native RAR accepted');
}
echo "Payload destination, privilege and RAR policy contracts passed\n";
