-- MySQL dump 10.13  Distrib 8.0.36
-- Host: localhost    Database: drupal
-- ------------------------------------------------------
-- Server version 8.0.36

DROP TABLE IF EXISTS `node_field_data`;
CREATE TABLE `node_field_data` (
  `nid` int unsigned NOT NULL,
  `vid` int unsigned NOT NULL,
  `type` varchar(32) NOT NULL,
  `langcode` varchar(12) NOT NULL,
  `status` tinyint(1) NOT NULL,
  `title` varchar(255) NOT NULL,
  `created` int NOT NULL,
  `changed` int NOT NULL,
  PRIMARY KEY (`nid`,`langcode`)
);
INSERT INTO `node_field_data` VALUES (1,1,'article','en',1,'Hello',1735689600,1735689600),(2,2,'article','en',1,'Foo',1735689600,1735689600),(3,3,'page','en',1,'About',1735689600,1735689600);

DROP TABLE IF EXISTS `users_field_data`;
CREATE TABLE `users_field_data` (
  `uid` int unsigned NOT NULL,
  `name` varchar(60) NOT NULL,
  PRIMARY KEY (`uid`)
);
INSERT INTO `users_field_data` VALUES (1,'admin');

DROP TABLE IF EXISTS `key_value`;
CREATE TABLE `key_value` (
  `collection` varchar(128) NOT NULL,
  `name` varchar(128) NOT NULL,
  `value` longblob NOT NULL,
  PRIMARY KEY (`collection`,`name`)
);

-- Dump completed on 2025-01-01 23:00:00
