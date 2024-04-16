#!/usr/bin/env python3

# See LICENSE file for copyright and license details.
# TUM CS Bot - https://github.com/ro-i/tumcsbot

from collections.abc import Iterable as IterableClass
from inspect import cleandoc
import re
from sqlite3 import IntegrityError
from typing import cast, Any, Callable, Iterable
from sqlalchemy import Column, String, Integer, ForeignKey, UniqueConstraint, update
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.ext.hybrid import hybrid_property
from tumcsbot.lib.regex import Regex

from tumcsbot.lib.response import Response
from tumcsbot.lib.client import AsyncClient
from tumcsbot.plugin import Event, Plugin, PluginCommandMixin
from tumcsbot.lib.command_parser import CommandParser
from tumcsbot.lib.db import DB, TableBase, Session, TableBase, serialize_model
from tumcsbot.plugin_decorators import *
from tumcsbot.plugins.usergroup import UserGroup, Usergroup
from tumcsbot.plugins.streamgroup import StreamGroup, Streamgroup
from tumcsbot.plugins.streams import Streams
from tumcsbot.lib.types import (
    DMError,
    DMMessage,
    DMResponse,
    PartialError,
    PartialSuccess,
    Privilege,
    UserNotPrivilegedException,
    response_type,
    ZulipUser,
    ZulipStream,
    YAMLSerializableMixin,
)


class CourseDB(TableBase):
    """Represents a course in the system."""

    __tablename__ = "Courses"

    CourseId = Column(Integer, primary_key=True, autoincrement=True)
    CourseName = Column(String, unique=True)
    CourseDescription = Column(String, nullable=True)

    Streams = Column(
        String,
        ForeignKey("StreamGroups.StreamGroupId", ondelete="CASCADE"),
        nullable=False,
    )

    Tutors = Column(
        Integer, ForeignKey("UserGroups.GroupId", ondelete="CASCADE"), nullable=False
    )

    TutorStream = Column(ZulipStream, nullable=False)
    InstructorStream = Column(ZulipStream, nullable=True)

    ModerationConfigId = Column(Integer, ForeignKey("ModerationConfig.ModerationConfigId"))

    _moderation_config = relationship("ModerationConfig", back_populates="_course")

    _streams = relationship(
        "StreamGroup", back_populates="_course", cascade="all, delete-orphan"
    )
    _tutors = relationship(
        "UserGroup", back_populates="_course", cascade="all, delete-orphan"
    )


# !!! INFO !!!! 
# relationships to add in UserGroup and StreamGroup when Course-Plugin is activated:
#
# StreamGroup:  _course = relationship("CourseDB", back_populates="_streams")
# Usergroup:    _course = relationship("CourseDB", back_populates="_tutors")
#

class Course(PluginCommandMixin, Plugin):
    """
    Manage Courses.
    """

    # ========================================================================================================================
    #       SUBCOMMANDS
    # ========================================================================================================================

    @command(name="list")
    @privilege(Privilege.ADMIN)
    async def _list(
        self,
        sender: ZulipUser,
        session: Session,
        args: CommandParser.Args,
        opts: CommandParser.Opts,
        message: dict[str, Any],
    ) -> AsyncGenerator[response_type, None]:
        pass

    @command
    @privilege(Privilege.ADMIN)
    @arg("name", str, description="The name of the Course.")
    @arg("emoji", Regex.get_emoji_name, description="The emoji to use for the Course.")
    @opt(
        "i",
        long_opt="instructors",
        description="The course has an additional Stream for Instructors.",
    )
    @opt(
        "s",
        long_opt="standard",
        description="Add standard steams to the course (Allgemein, Organisation, Feedback, Ankündigungen, Technik, Memes)."
    )
    async def create_empty(
        self,
        sender: ZulipUser,
        session: Session,
        args: CommandParser.Args,
        opts: CommandParser.Opts,
        message: dict[str, Any],
    ) -> AsyncGenerator[response_type, None]:
        """
        Create an empty course
        """
        name: str = args.name
        streamgroup_emoji: str = args.emoji

        if (
            session.query(CourseDB).filter(CourseDB.CourseName == name).first()
            is not None
        ):
            raise DMError(f"Course `{name}` already exists")
        
        if (
            session.query(StreamGroup).filter(StreamGroup.StreamGroupEmote==streamgroup_emoji).first()
            is not None
        ):
            raise DMError(f"Course with :{streamgroup_emoji}: already exists")

        try:
            # get a corresponding (empty) Streamgroup
            streamgroup_name: str = "streams_" + name
            streams: StreamGroup = Streamgroup._create_and_get_group(
                session, streamgroup_name, streamgroup_emoji
            )

            if opts.s:
                await Course.add_standard_streams(self, sender,session, message, name, streams)

            # get a corresponding (empty) Usergroup
            usergroup_name: str = "tutors_" + name
            tutors: Usergroup = Usergroup.create_and_get_group(session, usergroup_name)

            # get a corresponding (empty) Stream for Tutors
            tutors_stream_name: str = name + "-Tutoren"
            tutors_stream_desc: str = f"Interner Stream für {name}-Tutoren"
            await self.invoke_other_cmd(
                Streams.create,
                sender,
                session,
                message,
                name=tutors_stream_name,
                description=tutors_stream_desc,
            )
            tutors_stream: ZulipStream = ZulipStream(f"#**{tutors_stream_name}**")
            await tutors_stream

            # get a corresponding (empty) Stream for Instructors or None
            instructor_stream: ZulipStream | None = None
            if opts.i:
                instructor_stream_name: str = name + "-Instructors"
                instructor_stream_desc: str = f"Interner Stream für Intructors von {name}"
                await self.invoke_other_cmd(
                    Streams.create,
                    sender,
                    session,
                    message,
                    name=instructor_stream_name,
                    description=instructor_stream_desc,
                )
                instructor_stream: ZulipStream = ZulipStream(f"#**{tutors_stream_name}**")
                await instructor_stream
 
            # create and add a Course to the DB
            course: CourseDB = CourseDB(
                CourseName=name,
                Streams=streams,
                Tutors=tutors,
                TutorStream=tutors_stream,
                InstructorStream=instructor_stream
            )

            session.add(course)
            session.commit()

        except (DMError, sqlalchemy.exc.IntegrityError) as e:
            session.rollback()
            raise DMError(f"Something went wrong when creating the course `{name}` :botsweat:")

        yield DMResponse(f"Course `{name}` created :bothappypad:")

    @command
    @privilege(Privilege.ADMIN)
    @arg("name", str, description="The name of the Course")
    @arg("emoji", Regex.get_emoji_name, description="The emoji to use for the Streamgroup.")
    @opt(
        "s",
        long_opt="streamgroup",
        type=StreamGroup.StreamGroupId,
        description="The id of a Streamgroup containing the streams for this course.",
    )
    @opt(
        "t",
        long_opt="tutors",
        type=UserGroup.GroupName,
        description="The name of a Usergroup containing the tutors for this course.",
    )
    @opt(
        "tuts",
        long_opt="tutor_stream",
        type=ZulipStream,
        description="The course has an additional Stream for tutors.",
    )
    @opt(
        "ins",
        long_opt="instructor_stream",
        type=ZulipStream,
        description="The course has an additional Stream for Instructors.",
    )
    @opt(
        "s",
        long_opt="standard",
        description="Add standard steams to the course (Allgemein, Organisation, Feedback, Ankündigungen, Technik, Memes)."
    )
    async def create(
        self,
        sender: ZulipUser,
        session: Session,
        args: CommandParser.Args,
        opts: CommandParser.Opts,
        message: dict[str, Any],
    ) -> AsyncGenerator[response_type, None]:
        """
        Create a course with corresponding contents
        """
        name: str = args.name
        streamgroup_emoji: str = args.emoji

        if (
            session.query(CourseDB).filter(CourseDB.CourseName == name).first()
            is not None
        ):
            raise DMError(f"Course `{name}` already exists")
        
        if (
            session.query(StreamGroup).filter(StreamGroup.StreamGroupEmote==streamgroup_emoji).first()
            is not None
        ):
            raise DMError(f"Course with :{streamgroup_emoji}: already exists")

        try:
            # get corresponding Streamgroup
            streams: StreamGroup
            if opts.s:
                streams = opts.s
            else:
                streamgroup_name: str = "streams_" + name
                streams = Streamgroup._create_and_get_group(
                    session, streamgroup_name, streamgroup_emoji
                )

            if opts.s:
                await Course.add_standard_streams(self, sender,session, message, name, streams)

            # get corresponding Usergroup
            tutors: Usergroup
            if opts.t:
                tutors = opts.t
            else:
                usergroup_name: str = "tutors_" + name
                tutors = Usergroup.create_and_get_group(session, usergroup_name)

            # get corresponding Stream for Tutors
            tutors_stream: ZulipStream
            if opts.tuts:
                tutors_stream = opts.tuts
            else:
                tutors_stream_name: str = name + "-Tutoren"
                tutors_stream_desc: str = f"Interner Stream für {name}-Tutoren"
                await self.invoke_other_cmd(
                    Streams.create,
                    sender,
                    session,
                    message,
                    name=tutors_stream_name,
                    description=tutors_stream_desc,
                )
                tutors_stream = ZulipStream(f"#**{tutors_stream_name}**")
                await tutors_stream

            # get a corresponding (empty) Stream for Instructors or None
            instructor_stream: ZulipStream | None = None
            if opts.ins:
                instructor_stream = opts.ins
            else:
                instructor_stream_name: str = name + "-Instructors"
                instructor_stream_desc: str = f"Interner Stream für Intructors von {name}"
                await self.invoke_other_cmd(
                    Streams.create,
                    sender,
                    session,
                    message,
                    name=instructor_stream_name,
                    description=instructor_stream_desc,
                )
                instructor_stream = ZulipStream(f"#**{tutors_stream_name}**")
                await instructor_stream
 
            # create and add a Course to the DB
            course: CourseDB = CourseDB(
                CourseName=name,
                Streams=streams,
                Tutors=tutors,
                TutorStream=tutors_stream,
                InstructorStream=instructor_stream
            )

            session.add(course)
            session.commit()

            # subscribe tutors to Tutorstream
            tut_list: list[int] = Usergroup.get_user_ids_for_group(session,tutors)
            await sender.client.subscribe_users(user_ids=tut_list,
                                                stream_name=tutors_stream.name,
                                                allow_private_streams=True)

        except (DMError, sqlalchemy.exc.IntegrityError) as e:
            session.rollback()
            raise DMError(f"Something went wrong when creating the course `{name}` :botsweat:")

        yield DMResponse(f"Course `{name}` created :bothappypad:")

    @command
    @privilege(Privilege.ADMIN)
    @arg("course", type=CourseDB.CourseName,description="The name of the Course to delete.")
    @opt(
        "s",
        long_opt="streamgroup",
        description="Delete also Streamgroup",
    )
    @opt(
        "t",
        long_opt="tutors",
        description="Delete also Usergroup of Tutors.",
    )
    @opt(
        "tuts",
        long_opt="tutor_stream",
        description="Delete also Stream for Tutors.",
    )
    @opt(
        "ins",
        long_opt="instructor_stream",
        description="Delete also Stream for Instructors.",
    )
    async def delete(
        self,
        sender: ZulipUser,
        session: Session,
        args: CommandParser.Args,
        opts: CommandParser.Opts,
        message: dict[str, Any],
    ) -> AsyncGenerator[response_type, None]:
        
        course: CourseDB = args.course 
        streams_id: str = course.Streams
        tut_ug_id:int = course.Tutors
        tut_s: ZulipStream = course.TutorStream
        ins_s: ZulipStream = course.InstructorStream

        try:
            session.query(CourseDB).filter(CourseDB.CourseId==course.CourseId).delete()
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            raise DMError(f"Could not delete Course `{course.CourseName}`.") from e
        
        if opts.s:
            await self.invoke_other_cmd(
                Streamgroup.delete,
                sender,
                session,
                message,
                group_id=streams_id
                )
        
        if opts.t:
            tut_ug_name:str = Usergroup.get_name_by_id(session,tut_ug_id)
            await self.invoke_other_cmd(
                Usergroup.delete,
                sender,
                session,
                message,
                group=tut_ug_name
                )
            
        if opts.tuts:
            await sender.client.delete_stream(tut_s.id)

        if opts.ins:
            await sender.client.delete_stream(ins_s.id)

            
    @command
    @privilege(Privilege.ADMIN)
    @arg("course", type=CourseDB.CourseName,description="The name of the Course to delete.")
    @opt(
        "s",
        long_opt="streamgroup",
        type=StreamGroup.StreamGroupId,
        description="The id of an existing Streamgroup containing the streams for this course.",
    )
    @opt(
        "t",
        long_opt="tutors",
        type=UserGroup.GroupName,
        description="The name of an existing Usergroup containing the tutors for this course.",
    )
    @opt(
        "tuts",
        long_opt="tutor_stream",
        type=ZulipStream,
        description="The name of an existing Stream for Instructors.",
    )
    @opt(
        "ins",
        long_opt="instructor_stream",
        type=ZulipStream,
        description="The name of an existing Stream for Instructors.",
    )
    async def update(
        self,
        sender: ZulipUser,
        session: Session,
        args: CommandParser.Args,
        opts: CommandParser.Opts,
        message: dict[str, Any],
    ) -> AsyncGenerator[response_type, None]:
        """
        Update a course with corresponding contents
        """
        course: CourseDB = args.course 

        if opts.s:
            streams: StreamGroup = opts.s
            Course._update_streamgroup(course, session, streams)

        if opts.t:
            tutors: UserGroup = opts.t
            Course._update_tutorgroup(course, session, tutors)

        if opts.tuts:
            tutstream: ZulipStream = opts.tuts
            await Course._update_tutorstream(course, session,sender.client, tutstream)

        if opts.ins:
            insstream : ZulipStream = opts.ins
            await Course._update_instructorstream(course, session,sender.client, insstream)

        @command
        @privilege(Privilege.ADMIN)
        @arg("course", type=CourseDB.CourseName,description="The name of the Course to delete.")
        @opt("s", long_opt="streams", description="Remove the streams from the Course.")
        @opt("t", long_opt="tutors", description="Remove the tutors from the Course")
        async def clear(
            self,
            sender: ZulipUser,
            session: Session,
            args: CommandParser.Args,
            opts: CommandParser.Opts,
            message: dict[str, Any],
        ) -> AsyncGenerator[response_type, None]:
            """
            Clear a course (Strems/Tutors), but keep the underlying components (Streamgroup/UserGroup).
            """
            course: CourseDB = args.course



    # ========================================================================================================================
    #       CLASS METHODS
    # ========================================================================================================================
    @staticmethod
    async def add_standard_streams(plugin:Plugin, sender:ZulipUser, session:Session, message:dict[str, Any], name:str, sg:StreamGroup):
        # get a corresponding Streams
        allg_name: str = name + "-Allgemein"
        allg_desc: str = f"Willkommen im allgemeinen Zulip Stream von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=allg_name,
            description=allg_desc,
        )
        allg_stream: ZulipStream = ZulipStream(f"#**{allg_name}**")
        await allg_stream

        org_name: str = name + "-Organisation"
        org_desc: str = f"Willkommen im Orga-Zulip Stream von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=org_name,
            description=org_desc,
        )
        org_stream: ZulipStream = ZulipStream(f"#**{org_name}**")
        await org_stream

        fb_name: str = name + "-Feedback"
        fb_desc: str = f"Willkommen im Feedback Zulip Stream von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=fb_name,
            description=fb_desc,
        )
        fb_stream: ZulipStream = ZulipStream(f"#**{fb_name}**")
        await fb_stream

        ank_name: str = name + "-Ankündigungen"
        ank_desc: str = f"Willkommen im Zulip Stream für Ankündigungen von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=ank_name,
            description=ank_desc,
        )
        ank_stream: ZulipStream = ZulipStream(f"#**{ank_name}**")
        await ank_stream

        tech_name: str = name + "-Technik"
        tech_desc: str = f"Willkommen im Technik Zulip Stream von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=tech_name,
            description=tech_desc,
        )
        tech_stream: ZulipStream = ZulipStream(f"#**{tech_name}**")
        await tech_name

        memes_name: str = name + "-Memes"
        memes_desc: str = f"Willkommen im Meme Zulip Stream von dem Kurs {name}"
        await plugin.invoke_other_cmd(
            Streams.create,
            sender,
            session,
            message,
            name=memes_name,
            description=memes_desc,
        )
        memes_stream: ZulipStream = ZulipStream(f"#**{memes_name}**")
        await memes_stream

        Streamgroup._add_zulip_streams(session,
                                       [allg_stream, org_stream, fb_stream, ank_stream, tech_stream, memes_stream],
                                       sg)
        



    # ========================================================================================================================
    #       HELPER METHODS
    # ========================================================================================================================

    @staticmethod
    def _get_course_by_id(id: int, session: Session) -> CourseDB:
        result: CourseDB | None = None
        result = session.query(CourseDB).filter(CourseDB.CourseId == id).one_or_none()

        if result:
            return result

        raise DMError(
            f"Uuups, it looks like i could not find any Course associated with `{id}` :botsceptical:"
        )

    @staticmethod
    def _get_course_by_name(name: str, session: Session) -> CourseDB:
        result: CourseDB | None = None
        result = (
            session.query(CourseDB).filter(CourseDB.CourseName == name).one_or_none()
        )

        if result:
            return result

        raise DMError(
            f"Uuups, it looks like i could not find any Course associated with `{name}` :botsceptical:"
        )

    @staticmethod
    def _get_streamgroup(course: CourseDB, session: Session) -> StreamGroup:
        """
        Get the StreamGroup of a given Course.
        """
        id: int = course.Streams
        return session.query(StreamGroup).filter(StreamGroup.CouseId == id).one()

    @staticmethod
    def _get_tutorgroup(course: CourseDB, session: Session) -> UserGroup:
        """
        Get the Tutor-UserGroup of a given Course.
        """
        id: int = course.Tutors
        return session.query(UserGroup).filter(UserGroup.GroupId == id).one()

    @staticmethod
    async def _get_tutors(course: CourseDB, session: Session) -> list[ZulipUser]:
        """
        Get the Tutors of a Course a list of ZulipUsers.
        """
        ug: UserGroup = Course._get_tutorgroup(course, session)
        return Usergroup.get_users_for_group(session, ug)

    @staticmethod
    async def _get_streams(course: CourseDB, session: Session) -> list[ZulipStream]:
        """
        Get the Streams of a Course a list of ZulipStreams.
        """
        sg: StreamGroup = Course._get_streamgroup(course, session)
        return Streamgroup._get_streams(session, sg)
    
    @staticmethod
    def _update_streamgroup(course: CourseDB, session: Session, group:StreamGroup) -> None:
        """
        Set the StreamGroup of a given Course.
        """
        oldSG : StreamGroup = Course._get_streamgroup(course, session)
        if (oldSG == group):
            raise DMError("The given Streamgroup is already set for this course.")
        
        stmt = update(CourseDB).where(CourseDB.CourseId==course.CourseId).values(Streams=group)
        try:
            session.execute(stmt)
            session.query(StreamGroup).filter(StreamGroup.StreamGroupId==oldSG.StreamGroupId).delete()
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            raise DMError("Could not update Streamgroup :botsad:")
        


    @staticmethod
    def _update_tutorgroup(course: CourseDB, session: Session, group: UserGroup) -> None:
        """
        Set the Tutor-UserGroup of a given Course.
        """
        oldTG : UserGroup = Course._get_tutorgroup(course, session)
        if (oldTG == group):
            raise DMError("The given Usergroup is already set as Tutorgroup for this course.")
        
        stmt = update(CourseDB).where(CourseDB.CourseId==course.CourseId).values(Tutors=group)
        try:
            session.execute(stmt)
            session.query(UserGroup).filter(UserGroup.GroupId==oldTG.GroupId).delete()
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            raise DMError("Could not update Tutors :botsad:")

    @staticmethod
    async def _update_tutorstream(course:CourseDB, session:Session, client:AsyncClient, stream: ZulipStream) -> None:
        """
        Set the Tutor-Stream of a given Course.
        """
        oldTS : ZulipStream = course.TutorStream
        if (oldTS == stream):
            raise DMError("The given Stream is already set as Tutorstream for this course.")
        
        stmt = update(CourseDB).where(CourseDB.CourseId==course.CourseId).values(TutorStream=stream)
        try:
            session.execute(stmt)
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            raise DMError("SCould not update Tutor-Stream :botsad:")
        
        await client.delete_stream(oldTS.id)

    @staticmethod
    async def _update_instructorstream(course:CourseDB, session:Session, client:AsyncClient, stream: ZulipStream) -> None:
        """
        Set the Tutor-Stream of a given Course.
        """
        oldIS : ZulipStream = course.InstructorStream
        if (oldIS == stream):
            raise DMError("The given Stream is already set as Instructorstream for this course.")
        
        stmt = update(CourseDB).where(CourseDB.CourseId==course.CourseId).values(InstructorStream=stream)
        try:
            session.execute(stmt)
            session.commit()
        except sqlalchemy.exc.IntegrityError as e:
            session.rollback()
            raise DMError("Could not update Instructor-Stream :botsad:")
        
        await client.delete_stream(oldIS.id)

