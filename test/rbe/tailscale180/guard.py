from pathlib import Path
from prepare import replace
def guard(root):
    base=root/'pkg/tcpip/transport/tcp'
    p=base/'snd.go';s=p.read_text()
    s=replace(s,'spuriousRecovery bool','spuriousRecovery bool\n\tsackRecoveryInterrupted bool')
    s=replace(s,'s.FastRecovery.Last = s.SndNxt - 1\n\n\tif s.FastRecovery.Active {','if !s.sackRecoveryInterrupted {\n\t\ts.FastRecovery.Last = s.SndNxt - 1\n\t\ts.sackRecoveryInterrupted = s.FastRecovery.Active && s.ep.SACKPermitted\n\t}\n\n\tif s.FastRecovery.Active {')
    s=replace(s,'} else {\n\t\t// Detect loss by counting the duplicates','} else if !s.sackRecoveryInterrupted {\n\t\t// Detect loss by counting the duplicates')
    s=replace(s,'if s.FastRecovery.Last.LessThan(s.SndUna) {\n','if s.FastRecovery.Last.LessThan(s.SndUna) {\n\t\t\t\ts.sackRecoveryInterrupted = false\n')
    s=replace(s,'numLost > 0 && !s.FastRecovery.Active','numLost > 0 && !s.FastRecovery.Active && !s.sackRecoveryInterrupted')
    p.write_text(s)
    p=base/'rack.go';s=p.read_text();s=replace(s,'if !rc.snd.FastRecovery.Active {','if !rc.snd.FastRecovery.Active && !rc.snd.sackRecoveryInterrupted {');p.write_text(s)
if __name__=='__main__':
    guard(Path.cwd())
