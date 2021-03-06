import idaapi
from idaapi import *
from idc import *
from idautils import *

import hashlib
import traceback

from client import Client
from config import config
from comments import comments, comments_extra, NoChange

ida_reserved_prefix = (
    'sub_', 'locret_', 'loc_', 'off_', 'seg_', 'asc_', 'byte_', 'word_',
    'dword_', 'qword_', 'byte3_', 'xmmword_', 'ymmword_', 'packreal_',
    'flt_', 'dbl_', 'tbyte_', 'stru_', 'custdata_', 'algn_', 'unk_',
)

fhash = None
auto_wait = False
client = Client(**config)

### Helper Functions

def get_fhash():
    filename = idaapi.get_root_filename()
    if filename is None:
        return None
    with open(filename, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest().upper()

def get_can_addr(addr):
    """Convert an Effective Address to a canonical address."""
    return addr - get_imagebase()

def get_ea(addr):
    """Get Effective Address from a canonical address."""
    return addr + get_imagebase()

### Redis Functions ###

def onmsg_safe(key, data, replay=False):
    def tmp():
        try:
            onmsg(key, data, replay=replay)
        except Exception as e:
            print('error during callback for %s: %s' % (data.get('cmd'), e))
            traceback.print_exc()
    idaapi.execute_sync(tmp, MFF_WRITE)

def onmsg(key, data, replay=False):
    if key != fhash or key != get_fhash():
        print 'revsync: hash mismatch, dropping command'
        return

    if 'addr' in data:
        ea = get_ea(data['addr'])
    ts = int(data.get('ts', 0))
    cmd, user = data['cmd'], data['user']
    if cmd == 'comment':
        print 'revsync: <%s> %s %#x %s' % (user, cmd, data['addr'], data['text'])
        text = comments.set(ea, user, str(data['text']), ts)
        MakeComm(ea, text)
    elif cmd == 'extra_comment':
        print 'revsync: <%s> %s %#x %s' % (user, cmd, data['addr'], data['text'])
        text = comments_extra.set(ea, user, str(data['text']), ts)
        MakeRptCmt(ea, text)
    elif cmd == 'area_comment':
        print 'revsync: <%s> %s %s %s' % (user, cmd, data['range'], data['text'])
    elif cmd == 'rename':
        print 'revsync: <%s> %s %#x %s' % (user, cmd, data['addr'], data['text'])
        MakeName(ea, str(data['text']))
    elif cmd == 'join':
        print 'revsync: <%s> joined' % (user)
    else:
        print 'revsync: unknown cmd', data

def publish(data):
    if not autoIsOk():
        return
    if fhash == get_fhash():
        client.publish(fhash, data)

### IDA Hook Classes ###

def on_renamed(ea, new_name, local_name):
    if isLoaded(ea) and not new_name.startswith(ida_reserved_prefix):
        publish({'cmd': 'rename', 'addr': get_can_addr(ea), 'text': new_name})

def on_auto_empty_finally():
    global auto_wait
    if auto_wait:
        auto_wait = False
        on_load()

# These IDPHooks methods are for pre-IDA 7
class IDPHooks(IDP_Hooks):
    def renamed(self, ea, new_name, local_name):
        on_renamed(ea, new_name, local_name)
        return IDP_Hooks.renamed(self, ea, new_name, local_name)

    # TODO: make sure this is on 6.1
    def auto_empty_finally(self):
        on_auto_empty_finally()
        return IDP_Hooks.auto_empty_finally(self)

class IDBHooks(IDB_Hooks):
    def renamed(self, ea, new_name, local_name):
        on_renamed(ea, new_name, local_name)
        return IDB_Hooks.renamed(self, ea, new_name, local_name)

    def auto_empty_finally(self):
        on_auto_empty_finally()
        return IDB_Hooks.auto_empty_finally(self)

    def cmt_changed(self, ea, repeatable):
        cmt = GetCommentEx(ea, repeatable)
        try:
            changed = comments.parse_comment_update(ea, client.nick, cmt)
            publish({'cmd': 'comment', 'addr': get_can_addr(ea), 'text': changed or ''})
        except NoChange:
            pass
        return IDB_Hooks.cmt_changed(self, ea, repeatable)

    def extra_cmt_changed(self, ea, line_idx, repeatable):
        try:
            cmt = GetCommentEx(ea, repeatable)
            changed = comments_extra.parse_comment_update(ea, client.nick, cmt)
            publish({'cmd': 'extra_comment', 'addr': get_can_addr(ea), 'line': line_idx, 'text': changed or ''})
        except NoChange:
            pass
        return IDB_Hooks.extra_cmt_changed(self, ea, line_idx, repeatable)

    def area_cmt_changed(self, cb, a, cmt, repeatable):
        publish({'cmd': 'area_comment', 'range': [get_can_addr(a.startEA), get_can_addr(a.endEA)], 'text': cmt or ''})
        return IDB_Hooks.area_cmt_changed(self, cb, a, cmt, repeatable)

class UIHooks(UI_Hooks):
    pass

### Setup Events ###

def on_load():
    global fhash
    if fhash:
        client.leave(fhash)
    fhash = get_fhash()
    print 'revsync: connecting with', fhash
    client.join(fhash, onmsg_safe)

def wait_for_analysis():
    global auto_wait
    if autoIsOk():
        auto_wait = False
        on_load()
        return -1
    return 1000

def on_open():
    print 'revsync: file opened:', idaapi.get_root_filename()
    global auto_wait
    if autoIsOk():
        on_load()
        auto_wait = False
    else:
        auto_wait = True
        print 'revsync: waiting for auto analysis'
        if not hasattr(IDP_Hooks, 'auto_empty_finally'):
            idaapi.register_timer(1000, wait_for_analysis)

def on_close():
    global fhash
    if fhash:
        client.leave(fhash)
        fhash = None

hook1 = IDPHooks()
hook2 = IDBHooks()
hook3 = UIHooks()

def eventhook(event, old=0):
    if event == idaapi.NW_OPENIDB:
        on_open()
    elif event in (idaapi.NW_CLOSEIDB, idaapi.NW_TERMIDA):
        on_close()
    if event == idaapi.NW_TERMIDA:
        # remove hook on way out
        idaapi.notify_when(idaapi.NW_OPENIDB | idaapi.NW_CLOSEIDB | idaapi.NW_TERMIDA | idaapi.NW_REMOVE, eventhook)

def setup():
    if idaapi.get_root_filename():
        on_open()
    else:
        idaapi.notify_when(idaapi.NW_OPENIDB | idaapi.NW_CLOSEIDB | idaapi.NW_TERMIDA, eventhook)
    return -1

hook1.hook()
hook2.hook()
hook3.hook()
idaapi.register_timer(1000, setup)
print 'revsync: starting setup timer'
