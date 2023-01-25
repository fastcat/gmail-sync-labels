#!/usr/bin/python3

import importlib
import importlib.machinery

import email.header
import imaplib
import os
import pprint
import re
import shelve
import ssl
import pickle
import time
import datetime

oldconfig = None
newconfig = None

#FIXME: refactor to separate file to share code
class Gmail(imaplib.IMAP4_SSL):
    def __init__(self, cfg):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.set_default_verify_paths()
        # XXX: alternative, should work too:
        # ctx.load_verify_locations('/etc/ssl/certs/ca-certificates.crt')

        imaplib.IMAP4_SSL.__init__(self, 'imap.gmail.com', 993, ssl_context=ctx)

        # XXX: I have no idea how to check / if I need to check that thing. State from 2012-12-14.
        # MGL: set_default_verify_paths should do the work, as long as openssl is configured properly
        # assert self.sock.getpeercert() == {'subject': ((('countryName', 'US'),), (('stateOrProvinceName', 'California'),), (('localityName', 'Mountain View'),), (('organizationName', 'Google Inc'),), (('commonName', 'imap.gmail.com'),)), 'serialNumber': '3B73268B0000000068A5', 'subjectAltName': (('DNS', 'imap.gmail.com'),), 'version': 3, 'notBefore': 'Sep 12 11:55:49 2012 GMT', 'notAfter': 'Jun  7 19:43:27 2013 GMT', 'issuer': ((('countryName', 'US'),), (('organizationName', 'Google Inc'),), (('commonName', 'Google Internet Authority'),))}

        self.login(cfg.LOGIN, cfg.PASSWORD)

        assert 'X-GM-EXT-1' in self.capabilities
        
        # set a timeout for further ops so that it doesn't hang on network hiccups
        self.socket().settimeout(30.0)

    def selectfolder(self, folder, readonly=True):
        resp = self.select(folder, readonly)
        assert resp[0] == 'OK'

        total = int(resp[1][0])
        assert total > 0

        return total

#FIXME: refactor to separate class to share code
def download_labels_batch(gmail, start, count):
    # for 1,100 ask for 1:100, next time 101:200, etc.
    resp = gmail.fetch('%d:%d' % (start, start + count - 1), '(X-GM-LABELS UID BODY[HEADER.FIELDS (MESSAGE-ID)])')

    assert resp[0] == 'OK'

    """
    response here is ugly:

    [(b'1 (X-GM-LABELS () UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {61}',
      b'Message-ID: <a38097d40612071225s1e399c3eu@mail.gmail.com>\r\n\r\n'),
     b')',
     (b'2 (X-GM-LABELS () UID 2 BODY[HEADER.FIELDS (MESSAGE-ID)] {40}',
      b'Message-ID: <45787AFA.7020202@wp.pl>\r\n\r\n'),
     b')',
     ...
    ]
    
    And UID comes after X-GM-LABELs regardless of request order, wtf?
    """
    regexp = re.compile('(\d+) \(X-GM-LABELS \((.*)\) UID (\d+) BODY\[HEADER.FIELDS \(MESSAGE-ID\)\] {(\d+)}')

    # every even (2, 4, 6, 8, ...) item from response should be b')'
    for even_item in resp[1][1::2]:
        assert even_item == b')'

    # every odd (1, 3, 5, 7, ...) item should match regexp
    for odd_item in resp[1][::2]:
        try:
            imapid, labels, uid, payloadlen = regexp.match(odd_item[0].decode('utf-8')).groups()
        except AttributeError:
            print("%s\n%s" % (odd_item[0], odd_item[1]))
            raise

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

        yield uid, msgid, labels

#FIXME: refactor to separate class to share code
def download_labels(gmail, total):
    batch_size = 1000
    for start in range(1, total, batch_size):
        for uid, msgid, labels in download_labels_batch(gmail, start, batch_size):
            yield uid, msgid, labels

def map_labels(labels):
    for label in labels.split():
        #TODO: could keep most of these and map to things under [Gmail]/
        if label[0:3] == '"\\\\':
            assert label[-1:] == '"'
            continue
        yield label

def create_label_index(gmail, cfg):
    total = gmail.selectfolder(cfg.IMAP_FOLDER)
    index = dict()
    count = 0
    for uid, msgid, labels in download_labels(gmail, total):
        msglabels = index.setdefault(msgid, set())
        msglabels.update(map_labels(labels))
        count += 1
        if count % 100 == 0:
            print("Fetch: %7d / %7d" % (count, total), end='\r', flush=True)
    print("Fetch: %7d / %7d -- Done" % (count, total))
    return index

def apply_labels(gmail, cfg, index):
    total = gmail.selectfolder(cfg.IMAP_FOLDER)
    count = 0
    modified = 0
    added = 0
    start = time.time()
    for uid, msgid, labels in download_labels(gmail, total):
        count += 1
        msgwantlabels = index.get(msgid, set())
        if len(msgwantlabels) == 0:
            print("No labels for %s" % msgid)
        msghaslabels = set(map_labels(labels))
        msgneedlabels = msgwantlabels - msghaslabels
        if len(msgneedlabels) != 0:
            modified += 1
        #print("Message %s has %s, should have %s, add %s" % (msgid, msghaslabels, msgwantlabels, msgneedlabels))
        for l in msgneedlabels:
            type, data = gmail.uid('COPY', uid, l)
            assert type == 'OK'
            added += 1
            #print("%s" % (data,))
        # apply is slow, print all the time
        if True or count % 100 == 0:
            remaining = total - count
            if remaining <= 0:
            	# wtf?
            	remaining = 0
            now = time.time()
            if modified != 0:
                eta = start + (now - start) * remaining / modified
                assert eta >= start, "%f %f %d %d %d %d" % (start, now, total, count, remaining, modified)
                etastring = datetime.datetime.fromtimestamp(eta).strftime('%Y-%m-%d %H:%M:%S')
            else:
                etastring = '?'
            print("Apply: %7d (%8d) / %7d ETA %s" % (count, added, total, etastring), end='\r', flush=True)
    print("Apply: %7d (%8d) / %7d -- Done" % (count, added, total))

oldconfig = None
newconfig = None

def main():
    oldcfgname = sys.argv[1]
    newcfgname = sys.argv[2]

    global oldconfig
    global newconfig
    if os.path.isfile(oldcfgname):
    	oldconfig = importlib.machinery.SourceFileLoader('oldconfig', oldcfgname).load_module()
    else:
    	oldconfig = importlib.import_module(oldcfgname)
    if os.path.isfile(newcfgname):
    	newconfig = importlib.machinery.SourceFileLoader('newconfig', newcfgname).load_module()
    else:
    	newconfig = importlib.import_module(newcfgname)

    labelsfile = 'gmail-restore-labels.labels.pickle'
    index = None
    try:
        with open(labelsfile, 'rb') as f:
            index = pickle.load(f)
    except FileNotFoundError:
        print('No index file, will generate one')
        index = None
    if index is None:
        with Gmail(oldconfig) as oldgmail:
            index = create_label_index(oldgmail, oldconfig)
        with open(labelsfile, 'wb') as f:
            pickle.dump(index, f)
    
    with Gmail(newconfig) as newgmail:
        apply_labels(newgmail, newconfig, index)
    
    return

if __name__ == "__main__":
    main()
