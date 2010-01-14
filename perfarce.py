# Mercurial extension to push to and pull from Perforce depots.
#
# Copyright 2009-10 Frank Kingswood <frank@kingswood-consulting.co.uk>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2, incorporated herein by reference.

'''Push to or pull from Perforce depots

This extension modifies the remote repository handling so that repository
paths that resemble
    p4://p4server[:port]/clientname
cause operations on the named p4 client specification on the p4 server.
The client specification must already exist on the server before using
this extension. Making changes to the client specification Views causes
problems when synchronizing the repositories, and should be avoided.

Five built-in commands are overridden:

 outgoing  If the destination repository name starts with p4:// then
           this reports files affected by the revision(s) that are
           in the local repository but not in the p4 depot.

 push      If the destination repository name starts with p4:// then
           this exports changes from the local repository to the p4
           depot. If no revision is specified then all changes since
           the last p4 changelist are pushed. In either case, all
           revisions to be pushed are foled into a single p4 changelist.
           Optionally the resulting changelist is submitted to the p4
           server, controlled by the --submit option to push, or by
           setting
              --config perfarce.submit=True
           If the option
              --config perfarce.keep=False
           is False then after a successful submit the files in the
           p4 workarea will be deleted.

 pull      If the source repository name starts with p4:// then this
           imports changes from the p4 depot, automatically creating
           merges of changelists submitted by hg push.
           If the option
              --config perfarce.keep=False
           is False then the import does not leave files in the p4
           workarea, otherwise the p4 workarea will be updated
           with the new files.

 incoming  If the source repository name starts with p4:// then this
           reports changes in the p4 depot that are not yet in the
           local repository.

 clone     If the source repository name starts with p4:// then this
           creates the destination repository and pulls all changes
           from the p4 depot into it.
'''

from mercurial import cmdutil, commands, context, copies, extensions, hg, node, util
from mercurial.i18n import _
from hgext.convert.p4 import loaditer

import marshal, tempfile, os, re

def uisetup(ui):
    '''monkeypatch pull and push for p4:// support'''

    extensions.wrapcommand(commands.table, 'pull', pull)
    p = extensions.wrapcommand(commands.table, 'push', push)
    p[1].append(('', 'submit', None, 'for p4:// destination submit new changelist to server'))
    extensions.wrapcommand(commands.table, 'incoming', incoming)
    extensions.wrapcommand(commands.table, 'outgoing', outgoing)
    p = extensions.wrapcommand(commands.table, 'clone', clone)
    p[1].append(('', 'startrev', '', 'for p4:// source set initial revisions for clone'))

# --------------------------------------------------------------------------

class p4client:

    def __init__(self, ui, repo, path):
        'initialize a p4client class from the remote path'

        try:
            assert path.startswith('p4://')

            self.ui = ui
            self.repo = repo
            self.server = None
            self.client = None
            self.root = None
            self.keep = ui.configbool('perfarce', 'keep', True)

            # caches
            self.clientspec = {}
            self.usercache = {}
            self.wherecache = {}
            self.p4stat = None
            self.p4statdirty = False

            # helpers to parse p4 output
            self.re_type = re.compile('([a-z]+)?(text|binary|symlink|apple|resource|unicode|utf\d+)(\+\w+)?$')
            self.re_keywords = re.compile(r'\$(Id|Header|Date|DateTime|Change|File|Revision|Author):[^$\n]*\$')
            self.re_keywords_old = re.compile('\$(Id|Header):[^$\n]*\$')
            self.re_hgid = re.compile('{{mercurial (([0-9a-f]{40})(:([0-9a-f]{40}))?)}}')
            self.re_number = re.compile('.+ ([0-9]+) .+')
            self.actions = { 'edit':'M', 'add':'A', 'move/add':'A', 'delete':'R', 'move/delete':'R', 'purge':'R', 'branch':'A', 'integrate':'M' }

            maxargs = ui.config('perfarce', 'maxargs')
            if maxargs:
               self.MAXARGS = int(maxargs)
            elif os.name == 'posix':
               self.MAXARGS = 250
            else:
               self.MAXARGS = 25

            s, c = path[5:].split('/')
            if ':' not in s:
                s = '%s:1666' % s
            self.server = s
            if c:
                d = self.runs('client -o %s' % util.shellquote(c))
                for n in ['Root'] + ['AltRoots%d' % i for i in range(9)]:
                    if n in d and os.path.isdir(d[n]):
                        self.root = util.pconvert(d[n])
                        break
                if not self.root:
                    ui.note(_('the p4 client root must exist\n'))
                    assert False

                self.clientspec = d
                self.client = c
        except:
            ui.traceback()
            raise util.Abort(_('not a p4 repository'))


    def close(self):
        'clean up any client state'
        if self.p4statdirty:
            self._writep4stat()


    def latest(self, tags=False):
        '''Find the most recent changelist which has the p4 extra data which
        gives the p4 changelist it was converted from'''
        for rev in xrange(len(self.repo)-1, -1, -1):
            ctx = self.repo[rev]
            extra = ctx.extra()
            if 'p4' in extra:
                if tags:
                    # if there is a child with p4 tags then return the child revision
                    for ctx2 in ctx.children():
                        if ctx2.description().startswith('p4 tags\n') and ".hgtags" in ctx2:
                            ctx = ctx2
                            break
                return ctx.node(), int(extra['p4'])
        raise util.Abort(_('no p4 changelist revision found'))


    def decodetype(self, p4type):
        'decode p4 type name into mercurial mode string and keyword substitution regex'

        mode = ''
        keywords = None
        p4type = self.re_type.match(p4type)
        if p4type:
            flags = (p4type.group(1) or '') + (p4type.group(3) or '')
            if 'x' in flags:
                mode = 'x'
            if p4type.group(2) == 'symlink':
                mode = 'l'
            if 'ko' in flags:
                keywords = self.re_keywords_old
            elif 'k' in flags:
                keywords = self.re_keywords
        return mode, keywords


    SUBMITTED = -1
    def getpending(self, node):
        '''return p4 submission state for node: SUBMITTED, a changelist
        number if pending or None if not in a changelist'''
        if self.p4stat is None:
            self._readp4stat()
        return self.p4stat.get(node, None)

    def setpending(self, node, state):
        '''set p4 submission state for node: SUBMITTED, a changelist
        number if pending or None if not in a changelist'''
        r = self.getpending(node)
        if r != state:
            self.p4statdirty = True
            if state is None:
                del self.p4stat[node]
            else:
                self.p4stat[node] = state

    def getpendingdict(self):
        'return p4 submission state dictionary'
        if self.p4stat is None:
            self._readp4stat()
        return self.p4stat

    def _readp4stat(self):
        'read .hg/p4stat file'
        self.p4stat = {}
        try:
            for line in self.repo.opener('p4stat', 'r', text=True):
                line = line.strip().split(' ',1)
                if line[1].startswith('s'):
                    state = self.SUBMITTED
                else:
                    state = int(line[1])
                self.p4stat[line[0]] = state
        except:
            self.ui.traceback()
            self.ui.note(_('error reading p4stat\n'))
        self.ui.debug('read p4stat=%r\n' % self.p4stat)
        self.p4statdirty = False

    def _writep4stat(self):
        'write .hg/p4stat file'
        fd = self.repo.opener('p4stat', 'w', text=True)
        for n in self.p4stat:
            state = self.p4stat[n]
            if state == self.SUBMITTED:
                state = 'submitted'
            else:
                state = str(state)
            fd.write('%s %s\n' % (n, state))
        fd.close()
        self.p4statdirty = False
        self.ui.debug('write p4stat=%r\n' % self.p4stat)


    def run(self, cmd, files=[], abort=True):
        'Run a P4 command and yield the objects returned'

        c = ['p4', '-G']
        if self.server:
            c.append('-p')
            c.append(self.server)
        if self.client:
            c.append('-c')
            c.append(self.client)
        c.append(cmd)

        old=os.getcwd()
        try:
            if self.root:
                os.chdir(self.root)

            for i in range(0, len(files), self.MAXARGS) or [0]:
                cs = ' '.join(c + [util.shellquote(f) for f in files[i:i + self.MAXARGS]])
                if self.ui.debugflag: self.ui.debug('> %s\n' % cs)

                for d in loaditer(util.popen(cs, mode='rb')):
                    if self.ui.debugflag: self.ui.debug('< %r\n' % d)
                    code = d.get('code')
                    data = d.get('data')
                    if code is not None and data is not None:
                        if abort and code == 'error':
                            raise util.Abort('p4: %s' % data.strip())
                        elif code == 'info':
                            self.ui.note('p4: %s\n' % data)
                    yield d
        finally:
            os.chdir(old)


    def runs(self, cmd, one=True, **args):
        '''Run a P4 command and return the number of objects returned,
        or the object itself if exactly one was returned'''
        count = 0
        for d in self.run(cmd, **args):
            if not count:
                value = d
            count += 1
        if count == 1 and one:
            return value
        return count


    def getuser(self, user):
        'get full name and email address of user'
        r = self.usercache.get(user)
        if r:
            return r

        d = self.runs('user -o %s' % util.shellquote(user), abort=False)
        if 'Update' in d:
            try:
                r = '%s <%s>' % (d['FullName'], d['Email'])
                self.usercache[user] = r
                return r
            except:
                pass
        return user


    def where(self, files):
        '''Find local names for a list of files in the depot.
        Takes a list of tuples with the first element being the depot name
        and returns a modified list, with only entries for files that appear
        in the workspace, and appending the local name (relative to the root
        of the workarea) to each tuple.
        '''

        result = []
        fdict = {}

        for f in files:
            df = f[0]
            if df in self.wherecache:
                lf = self.wherecache[df]
                if lf is not None:
                    result.append(f + (lf,))
            else:
                fdict[df] = f

        if fdict:
            flist = sorted(f for f in fdict)

            n = 0
            for where in self.run('where', files=flist, abort=False):
                n += 1

                if n % 250 == 0:
                    if hasattr(self.ui, 'progress'):
                        self.ui.progress('p4 where', n, unit='files', total=len(files))
                    else:
                        self.ui.note('%d files\r' % n)
                        self.ui.flush()

                if where['code'] == 'error':
                    if where['severity'] == 2 and where['generic'] == 17:
                        # file not in client view
                        continue
                    else:
                        raise util.Abort(where['data'])

                df = where['depotFile']
                lf = where['path']
                if 'unmap' in where or lf.startswith('.hg'):
                    self.wherecache[df] = None
                else:
                    lf = util.pconvert(lf)
                    if lf.startswith('%s/' % self.root):
                        lf = lf[len(self.root) + 1:]
                    else:
                        raise util.Abort(_('invalid p4 local path %s') % lf)

                    self.wherecache[df] = lf
                    result.append(fdict[df] + (lf,))

            if hasattr(self.ui, 'progress'):
                self.ui.progress('p4 where', None)
            else:
                self.ui.note('%d files \n' % len(result))
                self.ui.flush()

            if n < len(flist):
                raise util.Abort(_('incomplete reply from p4, reduce maxargs'))
            elif n > len(flist):
                raise util.Abort(_('oversized reply from p4, remove duplicates from view'))

        return result


    def describe(self, change, files=None):
        '''Return p4 changelist description, user name and date.
        If the files argument is true, then also collect a list of 5-tuples
            (depotname, revision, type, action, localname)
        Retrieving the filenames is potentially very slow.
        '''

        self.ui.note(_('change %d\n') % change)
        d = self.runs('describe -s %d' % change)
        desc = d['desc']
        user = self.getuser(d['user'])
        date = (int(d['time']), 0)     # p4 uses UNIX epoch

        if files:
            files = []
            i = 0
            while ('depotFile%d' % i) in d and ('rev%d' % i) in d:
                files.append((d['depotFile%d' % i], int(d['rev%d' % i]), d['type%d' % i], self.actions[d['action%d' % i]]))
                i += 1
            files = self.where(files)

        return desc, user, date, files


    def getfile(self, entry, keepsync=True):
        '''Return contents of a file in the p4 depot at the given revision number
        Raises IOError if the file is deleted.
        '''

        if entry[3] == 'R':
            raise IOError()

        mode, keywords = self.decodetype(entry[2])

        if self.keep:
            if keepsync:
                self.runs('sync %s@%s' % (util.shellquote(entry[0]), entry[1]), abort=False)
            fn = os.sep.join([self.root, entry[-1]])
            fn = util.localpath(fn)
            if mode == 'l':
                contents = os.readlink(fn)
            else:
                contents = file(fn, 'rb').read()
        else:
            contents = []
            for d in self.run('print %s#%d' % (util.shellquote(entry[0]), entry[1])):
                code = d['code']
                if code == 'text' or code == 'binary':
                    contents.append(d['data'])

            contents = ''.join(contents)
            if mode == 'l' and contents.endswith('\n'):
                contents = contents[:-1]

        if keywords:
            contents = keywords.sub('$\\1$', contents)

        return mode, contents


    def labels(self, change):
        'Return p4 labels a.k.a. tags at the given changelist'

        tags = []
        for d in self.run('labels ...@%d,%d' % (change, change)):
            l = d.get('label')
            if l:
                tags.append(l)

        return tags


    @staticmethod
    def pullcommon(original, ui, repo, source, **opts):
        'Shared code for pull and incoming'

        source, revs, checkout = hg.parseurl(ui.expandpath(source or 'default'), opts.get('rev'))

        try:
            client = p4client(ui, repo, source)
        except:
            ui.traceback()
            return True, original(ui, repo, source, **opts)

        if len(repo):
            p4rev, p4id = client.latest(tags=True)
        else:
            p4rev = None
            p4id = 0

        changes = []
        for d in client.run('changes -s submitted -L ...@%d,#head' % p4id):
            c = int(d['change'])
            if c != p4id:
                changes.append(c)
        changes.sort()

        return False, (client, p4rev, p4id, changes)


    @staticmethod
    def pushcommon(out, original, ui, repo, dest, **opts):
        'Shared code for push and outgoing'

        dest, revs, co = hg.parseurl(ui.expandpath(dest or 'default-push',
                                                   dest or 'default'), opts.get('rev'))
        try:
            client = p4client(ui, repo, dest)
        except:
            ui.traceback()
            return True, original(ui, repo, dest, **opts)

        p4rev, p4id = client.latest(tags=True)
        ctx1 = repo[p4rev]
        rev = opts.get('rev')

        if rev:
            n1, n2 = cmdutil.revpair(repo, rev)
            if n2:
                ctx1 = repo[n1]
                ctx1 = ctx1.parents()[0]
                ctx2 = repo[n2]
            else:
                ctx2 = repo[n1]
                ctx1 = ctx2.parents()[0]
        else:
            ctx2 = repo['tip']

        nodes = repo.changelog.nodesbetween([ctx1.node()], [ctx2.node()])[0][1:]

        if not opts['force']:
            # trim off nodes at either end that have already been pushed
            trim = False
            for end in [0, -1]:
                while nodes:
                    n = repo[nodes[end]]
                    pending = client.getpending(n.hex())
                    if pending is not None:
                        del nodes[end]
                        trim = True
                    else:
                        break

            # recalculate the context
            if trim and nodes:
                ctx1 = repo[nodes[0]].parents()[0]
                ctx2 = repo[nodes[-1]]

            # check that remaining nodes have not already been pushed
            for n in nodes:
                n = repo[n]
                fail = False
                pending = client.getpending(n.hex())
                if pending is not None:
                    fail = True
                for ctx3 in n.children():
                    extra = ctx3.extra()
                    if 'p4' in extra:
                        fail = True
                        break
                if fail:
                    raise util.Abort(_('can not push, changeset %s is already in p4' % n))

        # find changed files
        if not nodes:
            mod = add = rem = []
            cpy = {}
        else:
            mod, add, rem = repo.status(node1=ctx1.node(), node2=ctx2.node())[:3]
            cpy = copies.copies(repo, ctx1, ctx2, repo[node.nullid])[0]

            # forget about copies with changes to the data
            forget = []
            for c in cpy:
                if ctx2[c].data() != ctx1[cpy[c]].data():
                    forget.append(c)
            for c in forget:
                del cpy[c]

            # remove .hg* files (mainly for .hgtags and .hgignore)
            for changes in [mod, add, rem]:
                i = 0
                while i < len(changes):
                    f = changes[i]
                    if f.startswith('.hg'):
                        del changes[i]
                    else:
                        i += 1

        if not (mod or add or rem):
            ui.status(_('no changes found\n'))
            return True, 0

        # create description
        desc = []
        for n in nodes:
            desc.append(repo[n].description())

        if len(nodes) > 1:
            h = [repo[nodes[0]].hex()]
        else:
            h = []
        h.append(repo[nodes[-1]].hex())

        desc='\n* * *\n'.join(desc) + '\n\n{{mercurial %s}}\n' % (':'.join(h))

        return False, (client, p4rev, p4id, nodes, ctx2, desc, mod, add, rem, cpy)


    def parsenodes(self, desc):
        'find revisions in p4 changelist description'
        m = self.re_hgid.search(desc)
        nodes = []
        if m:
            try:
                nodes = self.repo.changelog.nodesbetween(
                    [self.repo[m.group(2)].node()], [self.repo[m.group(4) or m.group(2)].node()])[0]
            except:
                self.ui.traceback()
                self.ui.note(_('ignoring hg revision range %s from p4\n' % m.group(1)))
        return nodes


    def submit(self, nodes, change):
        '''submit one changelist to p4, mark nodes in p4stat and optionally
        delete the files added or modified in the p4 workarea'''

        cl = None
        for d in self.run('submit -c %s' % change):
            if d['code'] == 'error':
                raise util.Abort(_('error submitting p4 change %s: %s') % (change, d['data']))
            cl = d.get('submittedChange', cl)

        for n in nodes:
            self.setpending(self.repo[n].hex(), self.SUBMITTED)

        self.ui.note(_('submitted changelist %s\n') % cl)

        if not self.keep:
            # delete the files in the p4 client directory
            self.runs('sync ...@0', abort=False)


# --------------------------------------------------------------------------

def incoming(original, ui, repo, source='default', **opts):
    '''show changes that would be pulled from the p4 source repository'''

    done, r = p4client.pullcommon(original, ui, repo, source, **opts)
    if done:
        return r

    client, p4rev, p4id, changes = r
    for c in changes:
        desc, user, date, files = client.describe(c, files=ui.verbose)
        tags = client.labels(c)

        ui.write(_('changelist:  %d\n') % c)
        # ui.write(_('branch:      %s\n') % branch)
        for tag in tags:
            ui.write(_('tag:         %s\n') % tag)
        # ui.write(_('parent:      %d:%s\n') % parent)
        ui.write(_('user:        %s\n') % user)
        ui.write(_('date:        %s\n') % util.datestr(date))
        if ui.verbose:
            ui.write(_('files:       %s\n') % ' '.join(f[-1] for f in files))

        if desc:
            if ui.verbose:
                ui.write(_('description:\n'))
                ui.write(desc)
                ui.write('\n')
            else:
                ui.write(_('summary:     %s\n') % desc.splitlines()[0])

        ui.write('\n')
    client.close()


def pull(original, ui, repo, source=None, **opts):
    '''Wrap the pull command to look for p4 paths, import changelists'''

    done, r = p4client.pullcommon(original, ui, repo, source, **opts)
    if done:
        return r

    client, p4rev, p4id, changes = r
    entries = {}
    c = 0

    def getfilectx(repo, memctx, fn):
        'callback to read file data'
        mode, contents = client.getfile(entries[fn], keepsync=False)
        return context.memfilectx(fn, contents, 'l' in mode, 'x' in mode, None)

    # for clone we support a --startrev option to fold initial changelists
    startrev = opts.get('startrev')
    if startrev:
        startrev = int(startrev)
        if startrev < 0:
            changes = changes[startrev:]
            startrev = changes[0]
        else:
            changes = [c for c in changes if c >= startrev]
            if changes[0] != startrev:
                raise util.Abort(_('changelist for --startrev not found'))
        if len(changes) < 2:
            raise util.Abort(_('with --startrev there must be at least two revisions to clone'))

    tags = {}

    try:
        for c in changes:
            desc, user, date, files = client.describe(c, files=not startrev)
            if startrev:
                files = []
                for d in client.run('files ...@%d' % startrev):
                    files.append((d['depotFile'], int(d['rev']), d['type'], client.actions[d['action']]))
                files = client.where(files)

            if client.keep:
                n = client.runs('sync -f',
                                files=[('%s#%d' % (f[-1], f[1])) for f in files],
                                one=False, abort=False)
                if n < len(files):
                    raise util.Abort(_('incomplete reply from p4, reduce maxargs'))

            entries = dict((f[-1],f) for f in files)

            nodes = client.parsenodes(desc)
            if nodes:
                parent = nodes[-1]
            else:
                parent = None

            if startrev:
                # no 'p4' data on first revision as it does not correspond
                # to a p4 changelist but to all of history up to a point
                extra = {}
                startrev = None
            else:
                extra = {'p4':c}

            ctx = context.memctx(repo, (p4rev, parent), desc,
                                 [f[-1] for f in files], getfilectx,
                                 user, date, extra)

            if nodes:
                for n in nodes:
                    client.setpending(repo[n].hex(), None)

            p4rev = repo.commitctx(ctx)
            ctx = repo[p4rev]

            for l in client.labels(c):
                tags[l] = (c, ctx.hex())

            ui.note(_('added changeset %d:%s\n') % (ctx.rev(), ctx))

    finally:
        if tags:
            p4rev, p4id = client.latest()
            ctx = repo[p4rev]

            if '.hgtags' in ctx:
                tagdata = [ctx.filectx('.hgtags').data()]
            else:
                tagdata = []

            desc = ['p4 tags']
            for l in sorted(tags):
                t = tags[l]
                desc.append('   %s @ %d' % (l, t[0]))
                tagdata.append('%s %s\n' % (t[1], l))

            def getfilectx(repo, memctx, fn):
                'callback to read file data'
                assert fn=='.hgtags'
                return context.memfilectx(fn, ''.join(tagdata), False, False, None)

            ctx = context.memctx(repo, (p4rev, None), '\n'.join(desc),
                                 ['.hgtags'], getfilectx)
            p4rev = repo.commitctx(ctx)
            ctx = repo[p4rev]
            ui.note(_('added changeset %d:%s\n') % (ctx.rev(), ctx))

        client.close()

    if opts['update']:
        return hg.update(repo, 'tip')


def clone(original, ui, source, dest=None, **opts):
    '''Wrap the clone command to look for p4 source paths, do pull'''

    try:
        client = p4client(ui, None, source)
    except:
        ui.traceback()
        return original(ui, source, dest, **opts)

    if dest is None:
        dest = hg.defaultdest(source)
        ui.status(_("destination directory: %s\n") % dest)
    else:
        dest = ui.expandpath(dest)
    dest = hg.localpath(dest)

    if not hg.islocal(dest):
        raise util.Abort(_("destination '%s' must be local") % dest)

    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise util.Abort(_("destination '%s' already exists") % dest)
        elif os.listdir(dest):
            raise util.Abort(_("destination '%s' is not empty") % dest)

    repo = hg.repository(ui, dest, create=True)

    opts['update'] = not opts['noupdate']
    opts['force'] = opts['rev'] = None

    try:
        r = pull(None, ui, repo, source=source, **opts)
    finally:
        fp = repo.opener("hgrc", "w", text=True)
        fp.write("[paths]\n")
        fp.write("default = %s\n" % source)
        fp.write("\n[perfarce]\n")
        fp.write("keep = %s\n" % client.keep)
        fp.close()

    return r


# --------------------------------------------------------------------------

def outgoing(original, ui, repo, dest=None, **opts):
    '''Wrap the outgoing command to look for p4 paths, report changes'''
    done, r = p4client.pushcommon(True, original, ui, repo, dest, **opts)
    if done:
        return r
    client, p4rev, p4id, nodes, ctx, desc, mod, add, rem, cpy = r

    if ui.quiet:
        # for thg integration until we support templates
        for n in nodes:
            ui.write('%s\n' % repo[n].hex())
    else:
        ui.write(desc)
        ui.write('\naffected files:\n')
        cwd = repo.getcwd()
        for char, files in zip('MAR', (mod, add, rem)):
            for f in files:
                ui.write('%s %s\n' % (char, repo.pathto(f, cwd)))
        ui.write('\n')
    client.close()


def push(original, ui, repo, dest=None, **opts):
    '''Wrap the push command to look for p4 paths, create p4 changelist'''

    done, r = p4client.pushcommon(False, original, ui, repo, dest, **opts)
    if done:
        return r
    client, p4rev, p4id, nodes, ctx, desc, mod, add, rem, cpy = r

    # sync to the last revision pulled/converted
    if client.keep:
        client.runs('sync ...@%d' % p4id, abort=False)
    else:
        client.runs('sync -k ...@%d' % p4id, abort=False)
        client.runs('sync -f', files=[('%s@%d' % (f, p4id)) for f in mod], abort=False)

    # attempt to reuse an existing changelist
    use = ''
    for d in client.run('changes -s pending -c %s -l' % client.client):
        if d['desc'] == desc:
            use = d['change']

    # revert any other changes to the files in existing changelist
    if use:
        ui.note(_('reverting: %s\n') % ' '.join(mod+add+rem))
        client.runs('revert -c %s' % use, files=mod + add + rem, abort=False)

    # get changelist data, and update it
    changelist = client.runs('change -o %s' % use)
    changelist['Description'] = desc

    fn = None
    try:
        # write changelist data to a temporary file
        fd, fn = tempfile.mkstemp(prefix='hg-p4-')
        fp = os.fdopen(fd, 'wb')
        marshal.dump(changelist, fp)
        fp.close()

        # update p4 changelist
        d = client.runs('change -i <%s' % util.shellquote(fn))
        data = d['data']
        if d['code'] == 'info':
            if not ui.verbose:
                ui.status('p4: %s\n' % data)
            if not use:
                m = client.re_number.match(data)
                if m:
                    use = m.group(1)
        else:
            raise util.Abort(_('error creating p4 change: %s') % data)

    finally:
        try:
            if fn: os.unlink(fn)
        except:
            pass

    if not use:
        raise util.Abort(_('did not get changelist number from p4'))

    # sort out the copies from the adds
    ntg = [(cpy[f], f) for f in add if f in cpy]
    add = [f for f in add if f not in cpy]

    # now add/edit/delete the files
    if mod:
        ui.note(_('opening for edit: %s\n') % ' '.join(mod))
        client.runs('edit -c %s' % use, files=mod)

    if mod or add:
        ui.note(_('retrieving file contents...\n'))

        for f in mod + add:
            out = os.path.join(client.root, f)
            ui.debug(_('writing: %s\n') % out)
            util.makedirs(os.path.dirname(out))
            fp = cmdutil.make_file(repo, out, ctx.node(), pathname=f)
            data = ctx[f].data()
            fp.write(data)
            fp.close()

    if add:
        ui.note(_('opening for add: %s\n') % ' '.join(add))
        client.runs('add -c %s' % use, files=add)

    if ntg:
        ui.note(_('opening for integrate: %s\n') % ' '.join(f[1] for f in ntg))
        for f in ntg:
            client.runs('integrate -c %s %s %s' % (use, f[0], f[1]))

    if rem:
        ui.note(_('opening for delete: %s\n') % ' '.join(rem))
        client.runs('delete -c %s' % use, files=rem)

    # submit the changelist to perforce if --submit was given
    if opts['submit'] or ui.configbool('perfarce', 'submit', default=False):
        client.submit(nodes, use)
    else:
        for n in nodes:
            client.setpending(repo[n].hex(), int(use))
        ui.note(_('pending changelist %s\n') % use)

    client.close()


# --------------------------------------------------------------------------

def submit(ui, repo, change=None, dest=None, **opts):
    '''submit a changelist to the p4 depot
    then update the local repository state.'''

    dest, revs, co = hg.parseurl(ui.expandpath(dest or 'default-push',
                                               dest or 'default'))

    client = p4client(ui, repo, dest)
    if change:
        change = int(change)
        desc, user, date, files = client.describe(change)
        nodes = client.parsenodes(desc)
        client.submit(nodes, change)
    elif opts['all']:
        changes = {}
        pending = client.getpendingdict()
        for i in pending:
            i = pending[i]
            if i != client.SUBMITTED:
                changes[i] = True
        changes = changes.keys()
        changes.sort()

        if len(changes) == 0:
            raise util.Abort(_('no pending changelists to submit'))

        for c in changes:
            desc, user, date, files = client.describe(c)
            nodes = client.parsenodes(desc)
            client.submit(nodes, c)

    else:
        raise util.Abort(_('no changelists specified'))

    client.close()


def revert(ui, repo, change=None, dest=None, **opts):
    '''revert a pending changelist and all opened files
    then update the local repository state.'''

    dest, revs, co = hg.parseurl(ui.expandpath(dest or 'default-push',
                                               dest or 'default'))

    client = p4client(ui, repo, dest)
    if change:
        changes = [int(change)]
    elif opts['all']:
        changes = {}
        pending = client.getpendingdict()
        for i in pending:
            i = pending[i]
            if isinstance(i,int):
                changes[i] = True
        changes = changes.keys()
        if len(changes) == 0:
            raise util.Abort(_('no pending changelists to revert'))
    else:
        raise util.Abort(_('no changelists specified'))

    for c in changes:
        try:
            desc, user, date, files = client.describe(c, files=True)
        except:
            files = None

        if files is not None:
            files = [f[-1] for f in files]
            ui.note(_('reverting: %s\n') % ' '.join(files))
            client.runs('revert', files=files, abort=False)
            ui.note(_('deleting: %d\n') % c)
            client.runs('change -d %d' %c , abort=False)

        reverted = []
        for rev in client.getpendingdict():
            if client.getpending(rev) == c:
                reverted.append(rev)

        for rev in reverted:
            client.setpending(rev, None)

    client.close()


def pending(ui, repo, dest=None, **opts):
    'report changelists already pushed and pending for submit in p4'

    dest, revs, co = hg.parseurl(ui.expandpath(dest or 'default-push',
                                               dest or 'default'))

    client = p4client(ui, repo, dest)

    changes = {}
    pending = client.getpendingdict()
    for i in pending:
        j = pending[i]
        if isinstance(j,int):
            changes.setdefault(j, []).append(i)
    keys = changes.keys()
    keys.sort()

    len = not ui.verbose and 12 or None
    for i in keys:
        if i == client.SUBMITTED:
            state = 'submitted'
        else:
            state = str(i)
        ui.write('%s %s\n' % (state, ' '.join(r[:len] for r in changes[i])))

    client.close()


def identify(ui, repo, *args, **opts):
    '''show p4 and hg revisions for the most recent p4 changelist

    With no revision, print a summary of the most recent revision
    in the repository that was converted from p4.
    Otherwise, find the p4 changelist for the revision given.
    '''

    rev = opts.get('rev')
    if rev:
        ctx = repo[rev]
        extra = ctx.extra()
        if 'p4' not in extra:
            raise util.Abort(_('no p4 changelist revision found'))
        changelist = int(extra['p4'])
    else:
        client = p4client(ui, repo, 'p4:///')
        p4rev, changelist = client.latest()
        ctx = repo[p4rev]

    num = opts.get('num')
    doid = opts.get('id')
    dop4 = opts.get('p4')
    default = not (num or doid or dop4)
    output = []

    if default or dop4:
        output.append(str(changelist))
    if num:
        output.append(str(ctx.rev()))
    if default or doid:
        output.append(str(ctx))

    ui.write("%s\n" % ' '.join(output))


cmdtable = {
    # 'command-name': (function-call, options-list, help-string)
    'p4pending':
        (   pending,
            [ ],
            'hg p4pending [p4://server/client]'),
    'p4revert':
        (   revert,
            [ ('a', 'all', None,   _('revert all pending changelists')) ],
            'hg p4revert [-a] changelist... [p4://server/client]'),
    'p4submit':
        (   submit,
            [ ('a', 'all', None,   _('submit all pending changelists')) ],
            'hg p4submit [-a] changelist... [p4://server/client]'),
    'p4identify':
        (   identify,
            [ ('r', 'rev', '',   _('identify the specified revision')),
              ('n', 'num', None, _('show local revision number')),
              ('i', 'id',  None, _('show global revision id')),
              ('p', 'p4',  None, _('show p4 revision number')),
            ],
            'hg p4identify [-inp] [-r REV]'
        )
}
