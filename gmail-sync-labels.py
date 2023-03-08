#!/usr/bin/python3 -B

"""
Copyright (C) 2013 Krzysztof Warzecha <kwarzecha7@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import importlib
import importlib.machinery

# just to force shelve to have the right one
# ndbm is unrelaible for large maildirs
import dbm.gnu

import email.header
import imaplib
import mailbox
import os
import pprint
import re
import shelve
import ssl
import sys

# prep global for later init
config = None

DATA_VERSION = 5

# utility helper
# GMail has started returning many headers with utf-8 encoding
# even if their value is 100% ascii
def header_to_string(headervalue):
    decoded = email.header.decode_header(headervalue)
    header = email.header.Header()
    for p in decoded:
        try:
            header.append(p[0], p[1])
        except UnicodeDecodeError:
            print("error handing header '%s'" % headervalue)
            print("unable to process header %s in encoding %s" %(repr(p[0]), p[1]))
            raise
    return str(header)

#FIXME: refactor to separate file to share code
class Gmail(imaplib.IMAP4_SSL):
    def __init__(self, login, password):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.set_default_verify_paths()
        # XXX: alternative, should work too:
        # ctx.load_verify_locations('/etc/ssl/certs/ca-certificates.crt')

        imaplib.IMAP4_SSL.__init__(self, 'imap.gmail.com', 993, ssl_context=ctx)

        # XXX: I have no idea how to check / if I need to check that thing. State from 2012-12-14.
        # MGL: set_default_verify_paths should do the work, as long as openssl is configured properly
        # assert self.sock.getpeercert() == {'subject': ((('countryName', 'US'),), (('stateOrProvinceName', 'California'),), (('localityName', 'Mountain View'),), (('organizationName', 'Google Inc'),), (('commonName', 'imap.gmail.com'),)), 'serialNumber': '3B73268B0000000068A5', 'subjectAltName': (('DNS', 'imap.gmail.com'),), 'version': 3, 'notBefore': 'Sep 12 11:55:49 2012 GMT', 'notAfter': 'Jun  7 19:43:27 2013 GMT', 'issuer': ((('countryName', 'US'),), (('organizationName', 'Google Inc'),), (('commonName', 'Google Internet Authority'),))}

        self.login(login, password)

        assert 'X-GM-EXT-1' in self.capabilities

        self.__message_from_imapid_re = re.compile('(\d+) \(X-GM-MSGID (\d+) X-GM-THRID (\d+) X-GM-LABELS \((.*)\) RFC822 {(\d+)}')

    def selectfolder(self, folder, readonly=True):
        resp = self.select(folder, readonly)
        assert resp[0] == 'OK'

        total = int(resp[1][0])
        assert total > 0

        return total

    # dead code?
    def listmessages(self, folder):
        print(' .. selecting mailbox')
        total = self.selectfolder(folder)

        print(' .. fetching remote index')
        resp = self.fetch('1:%d' % total, '(X-GM-MSGID)')

        assert resp[0] == 'OK'
        msglist = resp[1]

        headersexp = re.compile('(\d+) \(X-GM-MSGID (\d+)\)')

        print(' .. done')
        for msg in msglist:
            imapid, gmailid = headersexp.match(msg.decode('utf-8')).groups()

            yield int(imapid), int(gmailid)

    # dead code?
    def message_from_imapid(self, imapid):
        resp = self.fetch('%d' % imapid, '(X-GM-MSGID X-GM-THRID X-GM-LABELS RFC822)')

        assert resp[0] == 'OK'
        assert resp[1][1] == b')'

        imapid, msgid, thrid, labels, msglen = self.__message_from_imapid_re.match(resp[1][0][0].decode('utf-8')).groups()

        assert int(msglen) == len(resp[1][0][1])

        mail = email.message_from_bytes(resp[1][0][1])
        # gmail/getmail returns the mail header spelled a bit different
        mail.add_header('X-GMAIL-MSGID', msgid)
        mail.add_header('X-GMAIL-THRID', thrid)
        mail.add_header('X-GMAIL-LABELS', labels)

        return mail

class MaildirDatabase(mailbox.Maildir):
    """ you should realy consider using some kind of database for this """
    def __init__(self, path):
        mailbox.Maildir.__init__(self, path)
        self.lock()

        self.__message_ids = shelve.open(os.path.join(path, 'gmail-sync-labels.db'))
        if not '__VERSION' in self.__message_ids.keys() or self.__message_ids['__VERSION'] != DATA_VERSION:
            print('New database or new software version, re-indexing')
            # calling .clear() is very slow on a full database
            # a fully portable way of finding what files to delete is non-trivial(?)
            # but we can tell the underlying library to start fresh
            #self.__message_ids.clear()
            self.__message_ids.close()
            #os.unlink(os.path.join(path, 'gmail-sync-labels'))
            self.__message_ids = shelve.open(os.path.join(path, 'gmail-sync-labels.db'), flag='n')
            self.__message_ids['__VERSION'] = DATA_VERSION
        elif config.DEBUG:
            print('Opened DB with version %d, %d entries' % (self.__message_ids['__VERSION'], len(self.__message_ids)-1))
        # don't need this data yet
        #self.cache_message_info()
    
    def cache_message_info(self):
        # construct secondary indexes in memory
        self.__message_id_to_key = {}
        self.__gmail_id_to_key = {}
        # some messages don't have a message-id, we can't handle them yet
        # using gmail's message id may be a way around this
        self.__message_keys_without_id = set()
        # same problem with message ids that are duplicated
        self.__duplicated_message_ids = set()
        foundfatalerrors = False
        for key in self.__message_ids:
            if key == '__VERSION':
                continue
            info = self.__message_ids[key]
            gmailid = info['X-GMAIL-MSGID']
            if gmailid != None:
                if gmailid in self.__gmail_id_to_key.keys():
                    print('duplicate gmail id %s in %s and %s' % (gmailid, key, self.__gmail_id_to_key[gmailid]))
                    foundfatalerrors = True
                else:
                    self.__gmail_id_to_key[gmailid] = key
            messageids = info['Message-ID']
            for messageid in messageids:
                if messageid in self.__message_id_to_key.keys():
                    # duplicated
                    if config.DEBUG:
                        print('duplicate message id %s in %s and %s' % (messageid, key, self.__message_id_to_key[messageid]))
                    self.__duplicated_message_ids.add(messageid)
                    del self.__message_id_to_key[messageid]
                if messageid not in self.__duplicated_message_ids:
                    self.__message_id_to_key[messageid] = key
            if len(messageids) == 0:
                self.__message_keys_without_id.add(key)
        if config.DEBUG or config.MESSAGE_DETAILS:
            print('cached index: %d good message ids, %d duplicated ids, %d missing ids' %
                (len(self.__message_id_to_key), len(self.__duplicated_message_ids),
                len(self.__message_keys_without_id)))
        if foundfatalerrors:
            assert False, 'Found fatal errors, cannot continue'

    def init(self):
        i = 0
        seen = 0
        nomsgid = 0
        nogmailid = 0
        
        # message ids can have comments, extract just the <id@id> part
        # this is not perfect, comments might have strings that look like message ids
        # would need a full BNF parser for the productions in RFC2822 to handle this exactly right
        # need this because message ids gmail returns on queries have the comments stripped off
        extractmsgid = re.compile('(?s).*?(?-s:<.*>).*')
        
        # track what message keys still exist, remove others from the cache
        seenkeys = set()
        
        # process messages in deterministic order in debug mode
        # don't waste time sorting otherwise
        for key in sorted(self.iterkeys()) if config.DEBUG else self.iterkeys():
            seenkeys.add(key)
            i += 1

            if i % 100 == 0:
                yield i
            
            if i % 1000 == 0:
                if config.DEBUG:
                    print('snapshotting messages, seen %d of %d, missing %d/%d' % (seen, i, nomsgid, nogmailid))
                self.__message_ids.sync()

            # TODO: re-process messages with a message id but no gmail id,
            # as a prior run may have added the gmail id
            if key in self.__message_ids.keys():
                seen += 1
                continue
            
            messageids = []
            gmailid = None
            
            message = self.get(key)
            for k, v in message.items():
                ku = k.upper()
                # don't decode every header, both for performance
                # and to avoid getting stuck on bogusly encoded headers
                # that we don't care about to begin with
                if ku == 'MESSAGE-ID':
                    vv = header_to_string(v)
                    idx = extractmsgid.match(vv)
                    if idx == None:
                        if config.DEBUG:
                            print("Bogus looking message id '%s', tracking as-is" % (vv))
                        messageids.append(vv)
                    else:
                        messageids.append(idx.groups()[0])
                elif ku == 'X-GMAIL-MSGID':
                    # gmailid should never be duplicated
                    assert(gmailid == None)
                    vv = header_to_string(v)
                    gmailid = vv
            
            # printing these here instead of in cache_message_info means they only get
            # printed the first time we see the message
            if len(messageids) == 0:
                print('Message without id: %s' % (key,))
                nomsgid += 1
            elif config.DEBUG and len(messageids) > 1:
                print('Message with multiple IDs: %s' % key)
            if gmailid == None:
                print('Message without gmail id: %s' % (key,))
                nogmailid += 1
            
            self.__message_ids[key] = { 'Message-ID': messageids, 'X-GMAIL-MSGID': gmailid }
        
        # remove any deleted messages from index
        for key in list(self.__message_ids.keys() - seenkeys):
            if key == '__VERSION':
                continue
            if config.DEBUG:
                print('removing obsolete key %s' % key)
            del self.__message_ids[key]
        
        # update in-memory caches    
        self.cache_message_info()
        
        print('seen %d messages, processed %d messages' % (seen, i - seen))
        print('processed with no id: %d, no gmail id: %d' % (nomsgid, nogmailid))

    def close(self):
        self.__message_ids.close()
        self.unlock()

    def apply_labels(self, msgid, gmailid, gmailthreadid, labels):
        key = None
        if gmailid != None:
            try:
                key = self.__gmail_id_to_key[gmailid]
            except KeyError:
                if config.DEBUG or config.MESSAGE_DETAILS:
                    print("Can't find message by gmail id %s, retrying by message id %s" % (gmailid, msgid))
        if key == None and msgid != None:
            try:
                key = self.__message_id_to_key[msgid]
            except KeyError:
                if msgid in self.__duplicated_message_ids:
                    if config.DEBUG or config.MESSAGE_DETAILS:
                        print("skipping message with duplicated id: '%s'" % msgid)
        
        if key == None:
            if config.DEBUG or config.MESSAGE_DETAILS:
                print("no such message: '%s' / '%s'" % (msgid, gmailid))
            return -1
        
        msg = self[key]
        if msg['X-GMAIL-LABELS'] == labels:
            return 0
        
        if config.DEBUG:
            print("Updating message %s: '%s'/%s/%s => '%s'" % (key, msgid, gmailid, msg['X-GMAIL-LABELS'], labels))
        
        del msg['X-GMAIL-LABELS']
        msg['X-GMAIL-LABELS'] = labels

        self[key] = msg
        
        return 1

#FIXME: refactor to separate class to share code
def download_labels(gmail, total):
    """
    response here is ugly:

    [(b'1 (X-GM-MSGID 1222139561679786370 X-GM-LABELS () BODY[HEADER.FIELDS (MESSAGE-ID)] {61}',
      b'Message-ID: <a38097d40612071225s1e399c3eu@mail.gmail.com>\r\n\r\n'),
     b')',
     (b'2 (X-GM-MSGID 1222140200241725271 X-GM-LABELS () BODY[HEADER.FIELDS (MESSAGE-ID)] {40}',
      b'Message-ID: <45787AFA.7020202@wp.pl>\r\n\r\n'),
     b')',
     ...
    ]
    """
    regexp = re.compile('(\d+) \(X-GM-THRID (\d+) X-GM-MSGID (\d+) X-GM-LABELS \((.*)\) BODY\[HEADER.FIELDS \(MESSAGE-ID\)\] {(\d+)}')
    
    # gmail doesn't like doing large fetches, so batch it up into chunks
    chunk_size = 1000
    for chunk_start in range(1, total, chunk_size):
        # ranges in the imap fetch are inclusive, so care for fenceposts
        chunk_end = min(total, chunk_start + chunk_size - 1)
        # gmail gets cranky sometimes and just refuses to list some messages
        # if we get an error, skip one forwards
        # if the problem message is the last one, this will take forever
        # better would be to bisect the range, but that requires state tracking
        resp = None
        while resp == None and chunk_start <= chunk_end:
            resp = gmail.fetch('%d:%d' % (chunk_start, chunk_end), '(X-GM-THRID X-GM-MSGID X-GM-LABELS BODY[HEADER.FIELDS (MESSAGE-ID)])')
            if resp[0] == 'OK':
                break
            resp = None
            chunk_start += 1
        # no messages from this chunk were available, move on to the next chunk
        if resp == None:
            print("\nGave up fetching range [%d, %d]" % (chunk_start, chunk_end), file=sys.stderr)
            continue

        # every even (2, 4, 6, 8, ...) item from response should be b')'
        for even_item in resp[1][1::2]:
            assert even_item == b')'

        # every odd (1, 3, 5, 7, ...) item should match regexp
        for odd_item in resp[1][::2]:
            imapid, gmailthreadid, gmailid, labels, payloadlen = regexp.match(odd_item[0].decode('utf-8')).groups()

            assert int(payloadlen) == len(odd_item[1])

            try:
                msgid = odd_item[1].decode('utf-8').split()[1]
            except IndexError:
                if config.DEBUG or config.MESSAGE_DETAILS:
                    print('got message without Message-ID header: '
                          'gmail id %s, link: https://mail.google.com/mail/#all/%s'
                          % (gmailid, hex(int(gmailthreadid))[2:])
                    )
                #continue
                # allow update by gmail id
                msgid = None

            yield msgid, gmailid, gmailthreadid, labels

def main():
    cfgname = sys.argv[1]
    if cfgname == None:
    	cfgname = 'config'
    global config
    if os.path.isfile(cfgname):
    	config = importlib.machinery.SourceFileLoader('config', cfgname).load_module()
    else:
    	config = importlib.import_module(cfgname)
    
    print('opening maildir')
    db = MaildirDatabase(config.MAILDIR)

    total = len(db)

    updated_msgs = 0
    checked_msgs = 0
    errors = 0
    
    try:
        print('searching for new messages')

        for progress in db.init():
            if os.isatty(1):
                print('progress: %0.2f%%' % float(progress * 100 / total), end='\r', flush=True)
        
        if config.INDEX_ONLY:
            print('indexing complete')
            return

        print('connecting to gmail')
        gmail = Gmail(config.LOGIN, config.PASSWORD)

        #gmail.debug = 15;

        print('selecting mailbox')
        total = gmail.selectfolder(config.IMAP_FOLDER)
        i = 0

        print('downloading and applying labels for %d messages' % total)
        for msgid, gmailid, gmailthreadid, labels in download_labels(gmail, total):
            checked_msgs += 1
            i += 1
            if i % 10 == 0 and os.isatty(1):
                print('progress: %0.2f%% %d' % (float(i * 100 / total), checked_msgs), end='\r', flush=True)

            updaterc = db.apply_labels(msgid, gmailid, gmailthreadid, labels)
            if updaterc < 0:
                errors += 1
            else:
                updated_msgs += updaterc
    except imaplib.IMAP4.error as err:
        print('\nFailed with imap error:', file=sys.stderr)
        print(err, file=sys.stderr)
    finally:
        print('Updated %d/%d messages, %d errors' % (updated_msgs, checked_msgs, errors))
        # extra whitespace at end to ensure it fully overwrites progress line
        print('saving database ')
        db.close()

if __name__ == "__main__":
    sys.exit(main())

# vim: set ts=4 sw=4 et
