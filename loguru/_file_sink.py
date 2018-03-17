import datetime
import decimal
import glob
import numbers
import os
import random
import re
import shutil
import string
import time

import base36
import pendulum

from ._fast_now import fast_now

DAYS_NAMES = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']


class FileSink:

    def __init__(self, path, *, rotation=None, backups=None, compression=None, **kwargs):
        self.start_time = fast_now()
        self.start_time._FORMATTER = 'alternative'
        self.start_time._to_string_format = '%Y-%m-%d_%H-%M-%S'
        self.kwargs = kwargs.copy()
        self.kwargs.setdefault('mode', 'a')
        self.kwargs.setdefault('buffering', 1)
        self.path = str(path)
        self.file = None
        self.file_path = None
        self.created = 0
        self.rotation_time = self.start_time

        self.should_rotate = self.make_should_rotate_function(rotation)
        self.manage_backups = self.make_manage_backups_function(backups)
        self.compress_file = self.make_compress_file_function(compression)
        self.glob_pattern = self.make_glob_pattern(self.path)

        self.terminate(create_new=True)

        if self.should_rotate is None:
            self.write = self.file.write
        else:
            self.write = self.rotating_write

    def format_path(self):
        now = fast_now()
        now._FORMATTER = 'alternative'
        now._to_string_format = '%Y-%m-%d_%H-%M-%S'

        self.rotation_time._FORMATTER = 'alternative'
        self.rotation_time._to_string_format = '%Y-%m-%d_%H-%M-%S'

        record = {
            "time": now,
            "start_time": self.start_time,
            "rotation_time": self.rotation_time,
            "n": self.created,
            "n+1": self.created + 1,
        }

        return os.path.abspath(self.path.format_map(record))

    @staticmethod
    def make_glob_pattern(path):
        tokens = string.Formatter().parse(path)
        parts = (glob.escape(text) + '*' * (name is not None) for text, name, *_ in tokens)
        root, ext = os.path.splitext(''.join(parts))
        if ext:
            pattern = root + '.*'
        else:
            pattern = root + '*'
        return pattern

    def make_should_rotate_function(self, rotation):
        if rotation is None:
            return None
        elif isinstance(rotation, str):
            size = self.parse_size(rotation)
            if size is not None:
                return self.make_should_rotate_function(size)
            interval = self.parse_duration(rotation)
            if interval is not None:
                return self.make_should_rotate_function(interval)
            frequency = self.parse_frequency(rotation)
            if frequency is not None:
                return self.make_should_rotate_function(frequency)
            daytime = self.parse_daytime(rotation)
            if daytime is not None:
                day, time = daytime
                if day is None:
                    return self.make_should_rotate_function(time)
                elif time is None:
                    time = pendulum.parse('00:00', strict=True)
                day = getattr(pendulum, DAYS_NAMES[day])
                time_limit = self.start_time.at(time.hour, time.minute, time.second, time.microsecond)
                if time_limit <= self.start_time:
                    time_limit = time_limit.next(day, keep_time=True)
                self.rotation_time = time_limit
                def function(message):
                    nonlocal time_limit
                    record_time = message.record['time']
                    if record_time >= time_limit:
                        while time_limit <= record_time:
                            time_limit = time_limit.next(day, keep_time=True)
                        self.rotation_time = time_limit
                        return True
                    return False
            else:
                raise ValueError("Cannot parse rotation from: '%s'" % rotation)
        elif isinstance(rotation, (numbers.Real, decimal.Decimal)):
            size_limit = rotation
            def function(message):
                file = self.file
                file.seek(0, 2)
                return file.tell() + len(message) >= size_limit
        elif isinstance(rotation, datetime.time):
            time = pendulum.Time.instance(rotation)
            time_limit = self.start_time.at(time.hour, time.minute, time.second, time.microsecond)
            if time_limit <= self.start_time:
                time_limit = time_limit.add(days=1)
            self.rotation_time = time_limit
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    while time_limit <= record_time:
                        time_limit = time_limit.add(days=1)
                    self.rotation_time = time_limit
                    return True
                return False
        elif isinstance(rotation, datetime.timedelta):
            time_delta = pendulum.Interval.instance(rotation)
            time_limit = self.start_time + time_delta
            self.rotation_time = time_limit
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    while time_limit <= record_time:
                        time_limit += time_delta
                    self.rotation_time = time_limit
                    return True
                return False
        elif callable(rotation):
            time_limit = rotation(self.start_time)
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    time_limit = rotation(record_time)
                    self.rotation_time = time_limit
                    return True
                return False
        else:
            raise ValueError("Cannot infer rotation for objects of type: '%s'" % type(rotation).__name__)

        return function

    def make_manage_backups_function(self, backups):
        if backups is None:
            return None
        elif isinstance(backups, str):
            interval = self.parse_duration(backups)
            if interval is None:
                raise ValueError("Cannot parse backups from: '%s'" % backups)
            return self.make_manage_backups_function(interval)
        elif isinstance(backups, int):
            def function(logs):
                for log in sorted(logs, key=lambda log: (-os.stat(log).st_mtime, log))[backups:]:
                    os.remove(log)
        elif isinstance(backups, datetime.timedelta):
            seconds = backups.total_seconds()
            def function(logs):
                t = fast_now().timestamp()
                limit = t - seconds
                for log in logs:
                    if os.stat(log).st_mtime <= limit:
                        os.remove(log)
        elif callable(backups):
            function = backups
        else:
            raise ValueError("Cannot infer backups for objects of type: '%s'" % type(backups).__name__)

        return function

    def make_compress_file_function(self, compression):
        if compression is None:
            return None
        elif isinstance(compression, str):
            ext = compression.strip().lstrip('.')

            def compress_generic(opener, **kwargs):
                def compress(path_in, path_out):
                    with open(path_in, 'rb') as f_in:
                        with opener(path_out, 'wb', **kwargs) as f_out:
                            shutil.copyfileobj(f_in, f_out)
                return compress

            def compress_archive(mode):
                import tarfile
                def compress(path_in, path_out):
                    with tarfile.open(path_out, 'w:' + mode) as f_comp:
                        f_comp.add(path_in, os.path.basename(path_in))
                return compress

            def compress_zipped():
                import zlib, zipfile
                def compress(path_in, path_out):
                    with zipfile.ZipFile(path_out, 'w', compression=zipfile.ZIP_DEFLATED) as f_comp:
                        f_comp.write(path_in, os.path.basename(path_in))
                return compress

            if ext == 'gz':
                import zlib, gzip
                compress = compress_generic(gzip.open)
            elif ext == 'bz2':
                import bz2
                compress = compress_generic(bz2.open)
            elif ext == 'xz':
                import lzma
                compress = compress_generic(lzma.open, format=lzma.FORMAT_XZ)
            elif ext == 'lzma':
                import lzma
                compress = compress_generic(lzma.open, format=lzma.FORMAT_ALONE)
            elif ext == 'tar':
                compress = compress_archive('')
            elif ext == 'tar.gz':
                import zlib, gzip
                compress = compress_archive('gz')
            elif ext == 'tar.bz2':
                import bz2
                compress = compress_archive('bz2')
            elif ext == 'tar.xz':
                import lzma
                compress = compress_archive('xz')
            elif ext == 'zip':
                compress = compress_zipped()
            else:
                raise ValueError("Invalid compression format: '%s'" % ext)

            def compress_function(path_in):
                path_out = path_in + '.' + ext
                compress(path_in, path_out)
                os.remove(path_in)

            return compress_function
        elif callable(compression):
            return compression
        else:
            raise ValueError("Cannot infer compression for objects of type: '%s'" % type(compression).__name__)

    @staticmethod
    def parse_size(size):
        size = size.strip()
        reg = r'([e\+\-\.\d]+)\s*([kmgtpezy])?(i)?(b)'
        match = re.fullmatch(reg, size, flags=re.I)
        if not match:
            return None
        s, u, i, b = match.groups()
        try:
            s = float(s)
        except ValueError:
            raise ValueError("Invalid float value while parsing size: '%s'" % s)
        u = 'kmgtpezy'.index(u.lower()) + 1 if u else 0
        i = 1024 if i else 1000
        b = {'b': 8, 'B': 1}[b] if b else 1
        size = s * i**u / b

        return size

    @staticmethod
    def parse_duration(duration):
        duration = duration.strip()

        units = [
            ('y|years?', 31536000),
            ('mo|months?', 2628000),
            ('w|weeks?', 604800),
            ('d|days?', 86400),
            ('h|hours?', 3600),
            ('m|minutes?', 60),
            ('s|seconds?', 1),
            ('ms|milliseconds?', 0.001),
            ('us|microseconds?', 0.000001),
        ]

        reg = r'(?:([e\+\-\.\d]+)\s*([a-z]+)[\s\,]*)'
        if not re.fullmatch(reg + '+', duration, flags=re.I):
            return None

        seconds = 0

        for value, unit in re.findall(reg, duration, flags=re.I):
            try:
                value = float(value)
            except ValueError:
                raise ValueError("Invalid float value while parsing duration: '%s'" % value)

            try:
                unit = next(u for r, u in units if re.fullmatch(r, unit, flags=re.I))
            except StopIteration:
                raise ValueError("Invalid unit value while parsing duration: '%s'" % unit)

            seconds += value * unit

        return pendulum.Interval(seconds=seconds)

    @staticmethod
    def parse_frequency(frequency):
        frequency = frequency.strip().lower()
        function = None

        if frequency == 'hourly':
            function = lambda t: t.add(hours=1).start_of('hour')
        elif frequency == 'daily':
            function = '00:00'
        elif frequency == 'weekly':
            function = 'w0'
        elif frequency == 'monthly':
            function = lambda t: t.add(months=1).start_of('month')
        elif frequency == 'yearly':
            function = lambda t: t.add(years=1).start_of('year')

        return function

    @staticmethod
    def parse_daytime(daytime):
        daytime = daytime.strip()

        daytime_reg = re.compile(r'(.*?)\s+at\s+(.*)', flags=re.I)
        day_reg = re.compile(r'w\d+', flags=re.I)
        time_reg = re.compile(r'[\d\.\:\,]+(?:\s*[ap]m)?', flags=re.I)

        daytime_match = daytime_reg.fullmatch(daytime)
        if daytime_match:
            day, time = daytime_match.groups()
        elif time_reg.fullmatch(daytime):
            day, time = None, daytime
        elif day_reg.fullmatch(daytime) or daytime.upper() in DAYS_NAMES:
            day, time = daytime, None
        else:
            return None

        if day is not None:
            if day_reg.fullmatch(day):
                day = int(day[1:])
                if not 0 <= day <= 6:
                    raise ValueError("Invalid weekday index while parsing daytime: '%d'" % day)
            elif day.upper() in DAYS_NAMES:
                day = DAYS_NAMES.index(day.upper())
            else:
                raise ValueError("Invalid weekday value while parsing daytime: '%s'" % day)

        if time is not None:
            time_ = time
            try:
                time = pendulum.parse(time, strict=True)
            except Exception as e:
                raise ValueError("Invalid time while parsing daytime: '%s'" % time) from e
            else:
                if not isinstance(time, datetime.time):
                    raise ValueError("Cannot strictly parse time from: '%s'" % time_)

        return day, time

    def rotating_write(self, message):
        if self.should_rotate(message):
            compress = self.compress_file is not None
            manage = self.manage_backups is not None
            self.terminate(check_conflict=True, compress_file=compress, manage_backups=manage, create_new=True)
        self.file.write(message)

    def terminate(self, *, check_conflict=False, compress_file=False, manage_backups=False, create_new=False):
        old_file = self.file
        old_path = self.file_path

        self.file = None
        self.file_path = None

        if old_file is not None:
            old_file.close()

        new_path = self.format_path()

        if check_conflict and new_path == old_path:
            time_part = base36.dumps(int(time.time() * 1000))
            rand_part = base36.dumps(int(random.random() * 36**4))
            log_id = "{:0>8}{:0>4}".format(time_part, rand_part).upper()
            root, ext = os.path.splitext(old_path)
            renamed_path = root + '.' + log_id + ext
            os.rename(old_path, renamed_path)
            old_path = renamed_path

        if compress_file:
            self.compress_file(old_path)

        if manage_backups:
            logs = glob.glob(self.glob_pattern)
            self.manage_backups(logs)

        if create_new:
            new_dir = os.path.dirname(new_path)
            os.makedirs(new_dir, exist_ok=True)
            self.file = open(new_path, **self.kwargs)
            self.file_path = new_path
            self.created += 1

    def stop(self):
        compress = (self.compress_file is not None) and (self.should_rotate is None)
        manage = (self.manage_backups is not None) and (self.should_rotate is None)
        check = compress
        self.terminate(check_conflict=check, compress_file=compress, manage_backups=manage, create_new=False)
