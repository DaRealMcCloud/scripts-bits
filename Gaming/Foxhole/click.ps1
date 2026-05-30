#import mouse_event
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern void mouse_event(int flags, int dx, int dy, int cButtons, int info);' -Name U32 -Namespace W;
#left mouse click

$waitTime = 1000
Start-sleep -Seconds 5

while($true){
    $randomTime = Get-Random -Minimum 1 -Maximum 5
    Start-sleep -Milliseconds ($waitTime+$randomTime)
    [W.U32]::mouse_event(6,0,0,0,0);
}

