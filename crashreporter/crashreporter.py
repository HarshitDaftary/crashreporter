__author__ = 'calvin'

import traceback
import os
import glob
import datetime
import shutil
import smtplib
import time
import logging
import ftplib
import inspect
import jinja2

from threading import Thread
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders


class CrashReporter(object):
    """
    Create a context manager that emails or uploads a report by FTP with the traceback on a crash.
    It can be setup to do both, or just one of the upload methods.

    If a crash report fails to upload, the report is saved locally to the `report_dir` directory. The next time the
    CrashReporter starts up, it will attempt to upload all offline reports every `check_interval` seconds. After a
    successful upload the offline reports are deleted. A maximum of `offline_report_limit` reports are saved at any
    time. Reports are named crashreport.1, crashreport.2, crashreport.3 and so on. The most recent report is always
    crashreport.1.

    :param report_dir: Directory to save offline reports.
    :param check_interval: How often the to attempt to send offline reports
    :param logger: Optional logger to use.
    :param offline_report_limit: Number of offline reports to save.
    :param html: Create HTML reports (True) or plain text (False).

    """
    report_template = "crashreport%02d.txt"
    application_name = None
    application_version = None
    source_code_line_limit = 50

    def __init__(self, report_dir=None, offline_report_limit=10, html=False, check_interval=5*60, logger=None):
        self.html = html
        self._smtp = None
        self._ftp = None
        self._enabled = True
        self.logger = logger if logger else logging.getLogger(__name__)
        # Setup the directory used to store offline crash reports
        self.report_dir = report_dir
        self.check_interval = check_interval
        self.offline_report_limit = offline_report_limit
        self._watcher = None
        self._watcher_enabled = False
        if report_dir:
            if os.path.exists(report_dir):
                self.start_watcher()
            else:
                os.makedirs(report_dir)

    def setup_smtp(self, host, port, user, passwd, recipients, **kwargs):
        """
        Set up the crash reporter to send reports via email using SMTP
        :param host: SMTP host
        :param port: SMTP port
        :param user: sender email address
        :param passwd: sender email password
        :param recipients: list or comma separated string of recipients
        """
        self._smtp = kwargs
        self._smtp.update({'host': host, 'port': port, 'user': user, 'passwd': passwd, 'recipients': recipients})

    def setup_ftp(self, host, user, passwd, path, port=21, acct='', timeout=5, **kwargs):
        """
        Set up the crash reporter to upload reports via FTP.
        :param host: FTP host
        :param user: FTP user
        :param passwd: FTP password for user
        :param path: path to directory on the FTP server to upload files to
        :param port: FTP port
        :param acct: FTP account
        :param timeout: FTP timeout
        """
        self._ftp = kwargs
        self._ftp.update({'host': host, 'port': port, 'user': user, 'passwd': passwd, 'path': path, 'timeout': timeout,
                          'acct': acct})

    def enable(self):
        """
        Enable the crash reporter. CrashReporter is defaulted to be enabled on creation.
        """
        self.logger.info('CrashReporter: Enabled')
        self._enabled = True

    def disable(self):
        """
        Disable the crash reporter. No reports will be sent or saved.
        """
        self.logger.info('CrashReporter: Disabled')
        self._enabled = False
        self.stop_watcher()

    def start_watcher(self):
        """
        Start the watcher that periodically checks for offline reports and attempts to upload them.
        """
        if self._get_offline_reports():
            self.logger.info('CrashReporter: Starting watcher.')
            self._watcher = Thread(target=self._watcher_thread, name='offline_reporter')
            self._watcher.setDaemon(True)
            self._watcher_enabled = True
            self._watcher.start()

    def stop_watcher(self):
        """
        Stop the watcher thread that tries to send offline reports.
        """
        if self._watcher:
            self._watcher_enabled = False
            self.logger.info('CrashReporter: Stopping watcher.')

    def __enter__(self):
        self.enable()

    def __exit__(self, etype, evalue, tb):
        if self._enabled:
            if etype:
                self._etype = etype
                self._evalue = evalue
                self._tb = tb
                great_success = False
                if self._smtp is not None:
                    # Send the report via email
                    great_success |= self._sendmail(self.subject(), self.body(), self.attachments(), html=self.html)
                if self._ftp is not None:
                    # Send the report via FTP
                    great_success |= self._ftp_submit()

                # If both FTP and email sending fails, save the report
                if not great_success:
                    self._save_report()
        else:
            self.logger.info('CrashReporter: No crashes detected.')

    def subject(self):
        """
        Return a string to be used as the email subject line.
        """
        return 'Crash Report'

    def body(self):
        """
        Return a string to be used as the email body. Can be html if html is turned on.
        """
        # Get the last traceback
        tb_last = self._tb.tb_next
        while tb_last is not None:
            tb_last = tb_last.tb_next
        else:
            tb_last = self._tb
        _locals = tb_last.tb_frame.f_locals.copy()

        if self.html:
            dt = datetime.datetime.now()
            tb = [dict(zip(('file', 'line', 'module', 'code'),  t)) for t in traceback.extract_tb(self._tb)]
            error = traceback.format_exception_only(self._etype, self._evalue)[0]

            if 'self' in tb_last.tb_frame.f_locals:
                _locals = [('self', tb_last.tb_frame.f_locals['self'].__repr__())]
                scope_obj = getattr(tb_last.tb_frame.f_locals['self'], tb[-1]['module'])
                scope_lines, ln = inspect.getsourcelines(scope_obj)
                scope_lines = [(ln + i, 30 * (l.count('    ')-1), l.replace('    ', '')) for i, l in enumerate(scope_lines)]
            else:
                _locals = []
                for k, v in tb_last.tb_frame.f_locals.iteritems():
                    if k == 'self':
                        continue
                    try:
                        _locals.append((k, v.__repr__()))
                    except TypeError:
                        pass
                scope_lines = []
                with open(tb_last.tb_frame.f_locals['__file__'], 'r') as _f:
                    for c, l in enumerate(_f):
                        if c > tb_last.tb_lineno - self.source_code_line_limit:
                            scope_lines.append((c+1, 30 * (l.count('    ')-1), l.replace('    ', '')))

            fields = {'date': dt.strftime('%d %B %Y'),
                      'time': dt.strftime('%I:%M %p'),
                      'traceback': tb,
                      'error': error,
                      'localvars': _locals,
                      'app_name': self.application_name,
                      'app_version': self.application_version,
                      'source_code': scope_lines
                      }

            with open('./crashreporter/crashreport.html', 'r') as _f:
                template = jinja2.Template(_f.read())
            html_body = template.render(**fields)
            with open('report.html', 'w') as _g:
                _g.write(html_body)
            return html_body

        else:

            body = datetime.datetime.now().strftime('%d %B %Y, %I:%M %p\n')
            body += '\n'.join(traceback.format_exception(self._etype, self._evalue, self._tb))
            body += '\n'

            # Print a table of local variables
            limit = 25
            fmt = "{name:<25s}{value:<25s}\n"
            body += '-' * 90 + '\n'
            body += fmt.format(name='Variable', value='Value')
            body += '-' * 90 + '\n'
            body += fmt.format(name='self', value=_locals.pop('self'))
            count = 0
            for name, value in _locals.iteritems():
                body += fmt.format(name=name, value=value.__repr__())
                count += 1
                if count > limit:
                    break
            return body

    def attachments(self):
        """
        Generate and return a list of attachments to send with the report.
        :return: List of strings containing the paths to the files.
        """
        return []

    def delete_offline_reports(self):
        """
        Delete all stored offline reports
        """
        for report in self._get_offline_reports():
            os.remove(report)

    def _ftp_submit(self):
        """
        Upload the database to the FTP server. Only submit new information contained in the partial database.
        Merge the partial database back into master after a successful upload.
        """
        info = self._ftp
        try:
            ftp = ftplib.FTP()
            ftp.connect(host=info['host'], port=info['port'], timeout=info['timeout'])
            ftp.login(user=info['user'], passwd=info['passwd'], acct=info['acct'])
        except ftplib.all_errors as e:
            self.logger.error(e)
            self.stop_watcher()
            return False

        tmp = 'ftp_report.txt'
        self._write_report(tmp)

        ftp.cwd(info['path'])
        with open(tmp, 'rb') as _f:
            new_filename = self.report_template % (len(ftp.nlst()) + 1)
            ftp.storlines('STOR %s' % new_filename, _f)
            self.logger.info('CrashReporter: Submission to %s successful.' % info['host'])
            return True

    def _ftp_send_offline_reports(self):
        """
        Upload the database to the FTP server. Only submit new information contained in the partial database.
        Merge the partial database back into master after a successful upload.
        """
        info = self._ftp
        try:
            ftp = ftplib.FTP()
            ftp.connect(host=info['host'], port=info['port'], timeout=info['timeout'])
            ftp.login(user=info['user'], passwd=info['passwd'], acct=info['acct'])
        except ftplib.all_errors as e:
            self.logger.error(e)
            return False

        ftp.cwd(info['path'])
        for report in self._get_offline_reports():
            with open(report, 'rb') as _f:
                new_filename = self.report_template % (len(ftp.nlst()) + 1)
                ftp.storlines('STOR %s' % new_filename, _f)
        self.logger.info('CrashReporter: Submission to %s successful.' % info['host'])
        return True

    def _sendmail(self, subject, body, attachments=None, html=False):
        smtp = self._smtp
        msg = MIMEMultipart()
        if isinstance(smtp['recipients'], list) or isinstance(smtp['recipients'], tuple):
            msg['To'] = ', '.join(smtp['recipients'])
        else:
            msg['To'] = smtp['recipients']
        msg['From'] = smtp['user']
        msg['Subject'] = subject

        # Add the body of the message
        if html:
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body))

        # Add any attachments
        if attachments:
            for attachment in attachments:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(open(attachments, 'rb').read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition',
                                'attachment; filename="%s"' % os.path.basename(attachment))
                msg.attach(part)

        try:
            ms = smtplib.SMTP(smtp['host'], smtp['port'])
            ms.ehlo()
            ms.starttls()
            ms.ehlo()
            ms.login(smtp['user'], smtp['passwd'])
            ms.sendmail(smtp['user'], smtp['recipients'], msg.as_string())
            ms.close()
        except Exception as e:
            self.logger.error('CrashReporter: %s' % e)
            return False

        return True

    def _smtp_send_offline_reports(self):
        offline_reports = self._get_offline_reports()
        if offline_reports:
            # Add the body of the message
            body = 'Here is a list of crash reports that were stored offline.\n'
            body += '-------------------------------------------------\n'
            for report in offline_reports:
                with open(report, 'r') as _f:
                    text = _f.readlines()
                    body += ''.join(text)
                    body += '-------------------------------------------------\n'
            great_success = self._sendmail(self.subject(), body)
            if great_success:
                self.logger.info('CrashReporter: Offline reports sent.')
            return great_success

    def _write_report(self, path):
        # Write a new report
        with open(path, 'w') as _f:
            _f.write(self.body())

    def _save_report(self):
        """
        Save the crash report to a file. Keeping the last 5 files in a cyclical FIFO buffer.
        The newest crash report is 01
        """
        offline_reports = self._get_offline_reports()
        if offline_reports:
            # Increment the name of all existing reports
            for ii, report in enumerate(reversed(offline_reports)):
                n = int(report[-2:])
                new_name = os.path.join(self.report_dir, self.report_template % (n + 1))
                shutil.copy2(report, new_name)
            os.remove(report)
            # Delete the oldest report
            if len(offline_reports) >= self.offline_report_limit:
                oldest = os.path.join(self.report_dir, self.report_template % (self.offline_report_limit + 1))
                os.remove(oldest)
        new_report_path = os.path.join(self.report_dir, self.report_template % 1)
        self._write_report(new_report_path)

    def _get_offline_reports(self):
        return sorted(glob.glob(os.path.join(self.report_dir, "crashreport*")))

    def _watcher_thread(self):
        """
        Periodically attempt to upload the crash reports. If any upload method is successful, delete the saved reports.
        """
        great_success = False
        while not great_success:
            time.sleep(self.check_interval)
            if not self._watcher_enabled:
                break
            self.logger.info('CrashReporter: Attempting to send offline reports.')
            if self._smtp is not None:
                great_success |= self._smtp_send_offline_reports()
            if self._ftp is not None:
                # Send the report via FTP
                great_success |= self._ftp_send_offline_reports()

        if great_success:
            self.delete_offline_reports()
        self.logger.info('CrashReporter: Watcher stopped.')